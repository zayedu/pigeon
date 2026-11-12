import asyncio
import ipaddress
import json
from contextlib import asynccontextmanager
from math import ceil
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .models import (
    ACTIVE,
    MAX_FRAME,
    MAX_WAIT,
    Command,
    Context,
    Inspect,
    PigeonError,
    Submit,
    Turn,
)


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

    async def call(self, request: Command):
        async with self.lock:
            try:
                async with asyncio.timeout(MAX_WAIT + 5):
                    await self.socket.send(request.model_dump_json())
                    response = json.loads(await self.socket.recv())
            except (TimeoutError, asyncio.CancelledError):
                # A late reply could otherwise be mistaken for the next operation's reply.
                await self.socket.close()
                raise
        if "error" in response:
            raise PigeonError(response["error"], response["message"])
        return response["ok"]

    async def turn(self, request: Submit | Inspect) -> Turn:
        return Turn.model_validate(await self.call(request))

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
        until = asyncio.get_running_loop().time() + wait
        while turn.status in ACTIVE:
            remaining = ceil(until - asyncio.get_running_loop().time())
            if remaining <= 0:
                break
            turn = await self.turn(
                Inspect(
                    conversation_id=turn.request.conversation_id,
                    message_id=turn.request.id,
                    after=turn.status,
                    wait=min(MAX_WAIT, remaining),
                )
            )
        return turn


async def ask_agent(
    relay: str,
    token: str,
    message: str,
    *,
    to: str | None = None,
    context: Context | None = None,
    conversation_id: UUID | None = None,
    wait: int = 15,
) -> Turn:
    conversation_id = conversation_id or uuid4()
    message_id = uuid4()
    loop = asyncio.get_running_loop()
    until = loop.time() + wait
    submitted = False
    for attempt in range(3):
        try:
            async with open_client(relay, token) as client:
                if to is None:
                    previous = await client.turn(Inspect(conversation_id=conversation_id))
                    to = previous.request.recipient
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
            if exc.code not in ("connection", "storage_unavailable"):
                raise
            if attempt == 2:
                if submitted:
                    raise PigeonError(
                        exc.code,
                        f"{exc} Check status for conversation {conversation_id}, "
                        f"message {message_id}, before resending.",
                    ) from None
                raise
            await asyncio.sleep(0.25 * 2**attempt)
    raise AssertionError("Retry loop exhausted.")
