import asyncio
import subprocess
import sys
from uuid import uuid4

import pytest
from conftest import ALICE, BOB
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

from pigeon.client import open_client, relay_endpoint
from pigeon.models import MAX_FRAME, Context, Grant, Inspect, PigeonError, Submit, Take, command


def test_protocol_validation():
    frame = Submit(
        to=ALICE,
        message_id=uuid4(),
        conversation_id=uuid4(),
        message="Why cache misses?",
        context=Context(type="github_pr", repo="alice/project", number=42),
    )
    assert command.validate_json(frame.model_dump_json()) == frame
    data = frame.model_dump()
    for changes in (
        {"sender": BOB},
        {"message_id": "abc"},
        {"conversation_id": "abc"},
        {"to": "https://example.org"},
        {"message": ""},
        {"message": "x" * 4001},
        {"context": {"type": "github_pr", "repo": "../project", "number": 42}},
        {"context": {"type": "github_pr", "repo": "alice/project", "number": "42"}},
    ):
        with pytest.raises(ValidationError):
            command.validate_python(data | changes)
    with pytest.raises(PigeonError):
        relay_endpoint("ws://example.com")


def test_two_client_processes_exchange_messages(relay):
    async def exchange():
        async with open_client(relay.url, relay.alice) as receiver:
            await receiver.call(Grant(op="allow", sender=BOB))
        code = """
import asyncio, sys
from pigeon.client import open_client
from pigeon.models import Take, Finish

async def run():
    async with open_client(sys.argv[1], sys.argv[2]) as client:
        incoming = await client.call(Take(wait=5))
        assert incoming["request"]["sender"] == "@bob/claude"
        await client.call(Finish(message_id=incoming["request"]["id"],
                                 status="completed", content="Hello Bob"))
asyncio.run(run())
"""
        worker = subprocess.Popen([sys.executable, "-c", code, relay.url, relay.alice])
        try:
            async with open_client(relay.url, relay.bob) as caller:
                result = await caller.ask(ALICE, "Hello Alice", wait=5)
                assert result.status == "completed"
                assert result.response.content == "Hello Bob"
                assert result.response.sender == ALICE
                assert result.response.id != result.request.id
            assert worker.wait(timeout=5) == 0
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait()

    asyncio.run(exchange())


def test_auth_replay_and_reconnect(relay):
    async def exchange():
        request = Submit(to=ALICE, message="Hello", message_id=uuid4(), conversation_id=uuid4())
        async with open_client(relay.url, relay.alice) as receiver:
            async with open_client(relay.url, relay.bob) as caller:
                with pytest.raises(PigeonError, match="has not allowed"):
                    await caller.turn(request)
                await receiver.call(Grant(op="allow", sender=BOB))
                first = await caller.turn(request)
                assert first.status == "queued"
                assert first == await caller.turn(request)
                with pytest.raises(PigeonError, match="ID is already in use"):
                    await caller.turn(request.model_copy(update={"message": "changed"}))
        async with open_client(relay.url, relay.alice) as receiver:
            claimed = await receiver.call(Take(wait=0))
            assert claimed["status"] == "running"
        async with open_client(relay.url, relay.alice) as receiver:
            assert await receiver.call(Take(wait=0)) is None
        async with open_client(relay.url, relay.bob) as caller:
            assert (await caller.turn(Inspect(conversation_id=request.conversation_id))).status == (
                "running"
            )
        with pytest.raises(PigeonError, match="rejected"):
            async with open_client(relay.url, "x" * 43):
                pytest.fail("Invalid credential accepted.")

    asyncio.run(exchange())


def test_websocket_rejects_oversized_frames(relay):
    async def exchange():
        async with connect(
            relay.url + "/v1/ws", additional_headers={"Authorization": f"Bearer {relay.bob}"}
        ) as socket:
            await socket.send("x" * (MAX_FRAME + 1))
            with pytest.raises(ConnectionClosedError) as exc:
                await socket.recv()
            assert exc.value.rcvd.code == 1009

    asyncio.run(exchange())
