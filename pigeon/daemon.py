import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .client import open_client
from .models import MAX_TEXT, Finish, Inspect, PigeonError, Ping, Take, Turn

log = logging.getLogger(__name__)
RETRYABLE = {"connection", "storage_unavailable", "rate_limited"}


class Runtime(Protocol):
    async def run(self, turn: Turn, session: str | None) -> tuple[str, str]:
        """Return the persistent runtime session ID and the reply."""


class Echo:
    async def run(self, turn: Turn, session: str | None) -> tuple[str, str]:
        prefix = "Follow-up" if session else "First turn"
        return session or str(uuid4()), f"{prefix}: {turn.request.content}"


class Sessions:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                       conversation_id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL
                   )"""
            )

    def get(self, conversation_id: str) -> str | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT runtime_id FROM sessions WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        return row[0] if row else None

    def save(self, conversation_id: str, runtime_id: str):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                """INSERT INTO sessions VALUES (?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET runtime_id=excluded.runtime_id""",
                (conversation_id, runtime_id),
            )


@dataclass(frozen=True)
class Daemon:
    relay: str
    token: str
    agent: str
    allowed: frozenset[str]
    runtime: Runtime
    sessions: Sessions
    timeout: int = 120
    concurrency: int = 1

    async def retry[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        delay = 0.25
        while True:
            try:
                return await operation()
            except PigeonError as exc:
                if exc.code not in RETRYABLE:
                    raise
                log.debug("Relay unavailable. Retrying in %.2f seconds.", delay)
                await asyncio.sleep(delay)
                delay = min(5, delay * 2)

    async def watch(self, turn: Turn):
        async def observe():
            async with open_client(self.relay, self.token) as client:
                while True:
                    updated = await client.turn(
                        Inspect(
                            conversation_id=turn.request.conversation_id,
                            message_id=turn.request.id,
                            after="running",
                            wait=20,
                        )
                    )
                    if updated.status != "running":
                        return

        await self.retry(observe)

    async def execute(self, turn: Turn) -> Finish | None:
        request = turn.request

        def result(status, content):
            return Finish(message_id=request.id, status=status, content=content[:MAX_TEXT])

        if request.recipient != self.agent or request.sender not in self.allowed:
            return result("failed", "Recipient daemon has not allowed this sender.")

        conversation_id = str(request.conversation_id)
        try:
            session = self.sessions.get(conversation_id)
            if turn.follow_up and session is None:
                return result(
                    "failed", "Local runtime session is missing. Start a new conversation."
                )
            async with asyncio.timeout(self.timeout):
                observer = asyncio.create_task(self.watch(turn))
                execution = asyncio.create_task(self.runtime.run(turn, session))
                try:
                    done, _ = await asyncio.wait(
                        (observer, execution), return_when=asyncio.FIRST_COMPLETED
                    )
                    if observer in done:
                        observer.result()
                        return None
                    session, reply = execution.result()
                    self.sessions.save(conversation_id, session)
                    return result("completed", reply)
                finally:
                    for task in (observer, execution):
                        task.cancel()
                    await asyncio.gather(observer, execution, return_exceptions=True)
        except TimeoutError:
            return result("timed_out", "Local runtime exceeded its execution deadline.")
        except PigeonError as exc:
            return result("failed", str(exc))
        except Exception as exc:
            log.warning("Runtime turn %s failed (%s).", request.id, type(exc).__name__)
            return result("failed", "Local runtime failed. Check the recipient daemon logs.")

    async def worker(self):
        while True:

            async def take():
                async with open_client(self.relay, self.token) as client:
                    return await client.call(Take())

            work = await self.retry(take)
            if work is None:
                continue
            turn = Turn.model_validate(work)
            reply = await self.execute(turn)
            if reply is None:
                continue

            async def finish():
                async with open_client(self.relay, self.token) as client:
                    await client.call(reply)

            # Try once even at the deadline so the relay can return its terminal state.
            with suppress(TimeoutError):
                async with asyncio.timeout(30):
                    await self.retry(finish)

    async def run(self):
        async def verify():
            async with open_client(self.relay, self.token) as client:
                if (await client.call(Ping()))["agent"] != self.agent:
                    raise PigeonError(
                        "wrong_identity", "Daemon credential belongs to another agent."
                    )

        await self.retry(verify)
        log.info("Daemon connected as %s.", self.agent)
        workers = [asyncio.create_task(self.worker()) for _ in range(self.concurrency)]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
