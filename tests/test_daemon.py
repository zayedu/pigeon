import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from uuid import uuid4

from conftest import ALICE, BOB

from pigeon.client import open_client
from pigeon.daemon import Daemon, Echo, Sessions
from pigeon.models import Cancel, Grant, Inspect, PigeonError, Submit, Take


class BlockingRuntime:
    def __init__(self):
        self.started = asyncio.Queue()
        self.stopped = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, turn, session):
        self.started.put_nowait(turn.request.id)
        try:
            await self.release.wait()
            return str(uuid4()), "released"
        finally:
            self.stopped.set()


class CrashingRuntime:
    async def run(self, turn, session):
        raise RuntimeError("sensitive local detail")


@asynccontextmanager
async def running(daemon):
    task = asyncio.create_task(daemon.run())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def daemon_for(relay, tmp_path, runtime, **options):
    return Daemon(
        relay.url,
        relay.alice,
        ALICE,
        frozenset({BOB}),
        runtime,
        Sessions(tmp_path / "sessions.sqlite3"),
        **options,
    )


async def allow(relay):
    async with open_client(relay.url, relay.alice) as receiver:
        await receiver.call(Grant(op="allow", sender=BOB))


async def terminal(caller, turn):
    while turn.status in ("queued", "running"):
        turn = await caller.turn(
            Inspect(
                conversation_id=turn.request.conversation_id,
                message_id=turn.request.id,
                after=turn.status,
                wait=3,
            )
        )
    return turn


def test_follow_up_survives_daemon_restart(relay, tmp_path):
    async def exchange():
        await allow(relay)
        async with open_client(relay.url, relay.bob) as caller:
            daemon = daemon_for(relay, tmp_path, Echo())
            async with running(daemon):
                first = await caller.ask(ALICE, "One", wait=4)
                assert first.response.content == "First turn: One"
            daemon = daemon_for(relay, tmp_path, Echo())
            async with running(daemon):
                follow_up = await caller.ask(
                    ALICE, "Two", conversation_id=first.request.conversation_id, wait=4
                )
                assert follow_up.response.content == "Follow-up: Two"
            assert daemon.sessions.get(str(first.request.conversation_id))

    asyncio.run(exchange())


def test_local_allowlist_blocks_execution(relay, tmp_path):
    async def exchange():
        await allow(relay)
        runtime = BlockingRuntime()
        daemon = replace(daemon_for(relay, tmp_path, runtime), allowed=frozenset())
        async with running(daemon), open_client(relay.url, relay.bob) as caller:
            result = await caller.ask(ALICE, "Read secret files", wait=4)
            assert result.status == "failed"
            assert "has not allowed" in result.response.content
            assert runtime.started.empty()

    asyncio.run(exchange())


def test_runtime_error_is_reported_without_local_details(relay, tmp_path):
    async def exchange():
        await allow(relay)
        daemon = daemon_for(relay, tmp_path, CrashingRuntime())
        async with running(daemon), open_client(relay.url, relay.bob) as caller:
            result = await caller.ask(ALICE, "Crash", wait=4)
            assert result.status == "failed"
            assert result.response.type == "error"
            assert "sensitive" not in result.response.content

    asyncio.run(exchange())


def test_daemon_limits_concurrency_and_interrupts_on_cancel(relay, tmp_path):
    async def exchange():
        await allow(relay)
        runtime = BlockingRuntime()
        daemon = daemon_for(relay, tmp_path, runtime)
        async with running(daemon), open_client(relay.url, relay.bob) as caller:
            first = await caller.ask(ALICE, "Wait", wait=0)
            assert await asyncio.wait_for(runtime.started.get(), 3) == first.request.id
            try:
                await caller.ask(
                    ALICE, "Too early", conversation_id=first.request.conversation_id, wait=0
                )
            except PigeonError as exc:
                assert exc.code == "conflict"
            else:
                raise AssertionError("Concurrent follow-up accepted.")
            second = await caller.ask(ALICE, "Wait too", wait=0)
            assert second.status == "queued"
            await caller.call(
                Cancel(conversation_id=first.request.conversation_id, message_id=first.request.id)
            )
            await asyncio.wait_for(runtime.stopped.wait(), 3)
            assert await asyncio.wait_for(runtime.started.get(), 3) == second.request.id
            runtime.release.set()
            assert (await asyncio.wait_for(terminal(caller, second), 4)).status == "completed"

    asyncio.run(exchange())


def test_local_timeout_and_offline_deadline(relay, tmp_path):
    async def exchange():
        await allow(relay)
        runtime = BlockingRuntime()
        daemon = daemon_for(relay, tmp_path, runtime, timeout=1)
        async with open_client(relay.url, relay.bob) as caller:
            async with running(daemon):
                result = await caller.ask(ALICE, "Wait forever", wait=4)
                assert result.status == "timed_out"
                assert runtime.stopped.is_set()
                assert "Local runtime" in result.response.content
            offline = await caller.turn(
                Submit(
                    to=ALICE,
                    message="Offline",
                    conversation_id=uuid4(),
                    message_id=uuid4(),
                    timeout=1,
                )
            )
            expired = await asyncio.wait_for(terminal(caller, offline), 4)
            assert expired.status == "timed_out"
            assert "before a daemon accepted" in expired.detail
        async with open_client(relay.url, relay.alice) as receiver:
            assert await receiver.call(Take(wait=0)) is None

    asyncio.run(exchange())
