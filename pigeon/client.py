import asyncio
import ipaddress
import json
import random
from contextlib import asynccontextmanager
from math import ceil, floor
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .models import (
    ACTIVE,
    LEGACY_TEXT,
    MAX_FRAME,
    MAX_WAIT,
    Command,
    Context,
    Inspect,
    PigeonError,
    Ping,
    Share,
    Submit,
    Take,
    Turn,
)

# Seconds to back off before each retry of a transient relay storage error.
STORAGE_RETRIES = (0.2, 0.5, 1.2)


def retry_safe(request: Command) -> bool:
    """Whether repeating the frame cannot repeat its effect."""
    if isinstance(request, Take):
        # A storage error can follow a committed claim. Only a claim ID makes a retry
        # return that claim instead of taking the next request.
        return request.claim_id is not None
    if isinstance(request, Share):
        return request.resume
    # Reads, and writes keyed by message, conversation, or sender, are idempotent.
    return True


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def relay_endpoint(relay: str) -> str:
    try:
        url = urlsplit(relay)
        if (
            url.hostname
            and url.username is None
            and url.password is None
            and url.path in ("", "/")
            and not url.query
            and not url.fragment
            and (url.scheme == "wss" or (url.scheme == "ws" and is_loopback(url.hostname)))
        ):
            _ = url.port
            return url._replace(path="/v1/ws").geturl()
    except ValueError:
        pass
    raise PigeonError("invalid_relay", "Use wss://host[:port], or ws:// for a loopback relay.")


@asynccontextmanager
async def open_client(relay: str, token: str):
    try:
        async with connect(
            relay_endpoint(relay),
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=5,
            close_timeout=1,
            max_size=MAX_FRAME,
            compression=None,
            proxy=None,
        ) as socket:
            yield Client(socket)
    except InvalidStatus as exc:
        if exc.response.status_code == 403:
            raise PigeonError(
                "unauthenticated", "Relay rejected the credential or connection limit was reached."
            ) from None
        detail = f"Relay returned HTTP {exc.response.status_code}."
        raise PigeonError("connection", detail) from None
    except (OSError, TimeoutError, WebSocketException) as exc:
        detail = f"Relay connection failed ({type(exc).__name__})."
        raise PigeonError("connection", detail) from None


class Client:
    def __init__(self, socket: ClientConnection):
        self.socket = socket
        self.lock = asyncio.Lock()

    async def call(self, request: Command, *, until: float | None = None):
        """Send one frame. Retry transient storage errors, finishing by `until` if given."""
        loop = asyncio.get_running_loop()
        for delay in (*STORAGE_RETRIES, None):
            try:
                return await self.send(request)
            except PigeonError as exc:
                if exc.code != "storage_unavailable" or delay is None or not retry_safe(request):
                    raise
                delay *= random.uniform(0.8, 1.2)
                if until is not None:
                    left = until - loop.time() - delay
                    if left < 0:
                        raise
                    if hasattr(request, "wait"):
                        wait = min(request.wait, floor(left))
                        request = request.model_copy(update={"wait": wait})
                await asyncio.sleep(delay)
        raise AssertionError("Retry loop exhausted.")

    async def send(self, request: Command):
        async with self.lock:
            try:
                async with asyncio.timeout(MAX_WAIT + 5):
                    # Omit unset fields so relays that predate them accept the frame.
                    await self.socket.send(request.model_dump_json(exclude_none=True))
                    response = json.loads(await self.socket.recv())
            except (TimeoutError, asyncio.CancelledError):
                # A late reply could otherwise be mistaken for the next operation's reply.
                await self.socket.close()
                raise
        if "error" in response:
            text = getattr(request, "message", None) or getattr(request, "content", None)
            if response["error"] == "invalid_frame" and len(text or "") > LEGACY_TEXT:
                raise PigeonError(
                    "message_limit",
                    f"This relay accepts at most {LEGACY_TEXT:,} characters per message. "
                    "Split the message.",
                )
            raise PigeonError(response["error"], response["message"], response.get("data"))
        return response["ok"]

    async def turn(self, request: Submit | Inspect, *, until: float | None = None) -> Turn:
        return Turn.model_validate(await self.call(request, until=until))

    async def ask(
        self,
        to: str,
        message: str,
        *,
        context: Context | None = None,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        wait: int = 15,
    ) -> Turn:
        turn = await self.turn(
            Submit(
                to=to,
                message=message,
                context=context,
                conversation_id=conversation_id or uuid4(),
                message_id=message_id or uuid4(),
            )
        )
        if not wait or turn.status not in ACTIVE:
            return turn
        return await self.wait_for(
            turn.request.conversation_id, turn.request.id, wait, current=turn.status
        )

    async def wait_for(
        self,
        conversation_id: UUID,
        message_id: UUID,
        wait: int,
        *,
        after: str | None = None,
        current: str = "queued",
    ) -> Turn:
        """Long-poll one request for up to `wait` seconds.

        Without `after`, return at a terminal state. With `after`, return once the request
        leaves that state. At the deadline, return the current state. `current` is the
        state the caller last saw, so the first relay call already waits.
        """
        loop = asyncio.get_running_loop()
        until = loop.time() + wait
        state = after or current
        while True:
            remaining = max(0, ceil(until - loop.time()))
            turn = await self.turn(
                Inspect(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    after=state,
                    wait=min(MAX_WAIT, remaining),
                ),
                until=until,
            )
            if (
                turn.status not in ACTIVE
                or (after is not None and turn.status != after)
                or loop.time() >= until
            ):
                return turn
            state = turn.status

    async def status(
        self,
        conversation_id: UUID,
        message_id: UUID | None = None,
        *,
        wait: int = 0,
        after: str | None = None,
    ) -> Turn:
        """Read a request, or the latest one, and optionally wait as in wait_for."""
        if wait and message_id is not None:
            return await self.wait_for(conversation_id, message_id, wait, after=after)
        turn = await self.turn(Inspect(conversation_id=conversation_id, message_id=message_id))
        if wait and turn.status in ACTIVE and after in (None, turn.status):
            turn = await self.wait_for(
                conversation_id, turn.request.id, wait, after=after, current=turn.status
            )
        return turn

    async def take(self, wait: int, claim_id: UUID, *, until: float | None = None) -> Turn | None:
        try:
            result = await self.call(Take(wait=wait, claim_id=claim_id), until=until)
        except PigeonError as exc:
            if exc.code != "invalid_frame":
                raise
            # A relay that predates claim IDs rejects the field. Its claims are not retried.
            result = await self.call(Take(wait=wait), until=until)
        return Turn.model_validate(result) if result else None

    async def receive(self, wait: int, claim_id: UUID) -> Turn | None:
        """Claim the next queued request, waiting up to `wait` seconds in relay-sized steps."""
        loop = asyncio.get_running_loop()
        until = loop.time() + wait
        while True:
            remaining = max(0, round(until - loop.time()))
            turn = await self.take(min(MAX_WAIT, remaining), claim_id, until=until)
            if turn is not None or remaining <= MAX_WAIT:
                return turn


async def ask_agent(
    relay: str,
    token: str,
    message: str,
    *,
    to: str | None = None,
    context: Context | None = None,
    conversation_id: UUID | None = None,
    message_id: UUID | None = None,
    wait: int = 15,
) -> Turn:
    conversation_id = conversation_id or uuid4()
    message_id = message_id or uuid4()
    loop = asyncio.get_running_loop()
    until = loop.time() + wait
    submitted = False
    for attempt in range(3):
        try:
            async with open_client(relay, token) as client:
                if to is None:
                    me = (await client.call(Ping()))["agent"]
                    previous = await client.turn(Inspect(conversation_id=conversation_id))
                    parties = {previous.request.sender, previous.request.recipient}
                    if me not in parties:
                        raise PigeonError("not_found", "Conversation or message not found.")
                    to = (parties - {me}).pop()
                submitted = True
                return await client.ask(
                    to,
                    message,
                    context=context,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    wait=max(0, ceil(until - loop.time())),
                )
        except PigeonError as exc:
            # Client.call already retries storage errors on an open connection.
            if exc.code not in ("connection", "storage_unavailable"):
                raise
            if attempt == 2 or exc.code == "storage_unavailable":
                if submitted:
                    raise PigeonError(
                        exc.code,
                        f"{exc} Check status for conversation {conversation_id}, "
                        f"message {message_id}, before resending.",
                    ) from None
                raise
            await asyncio.sleep(0.25 * 2**attempt)
    raise AssertionError("Retry loop exhausted.")
