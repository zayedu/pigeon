import asyncio
import secrets
from uuid import UUID, uuid4

import pytest

from pigeon.client import STORAGE_RETRIES, Client, open_client
from pigeon.models import (
    Finish,
    History,
    Inspect,
    ListCalls,
    PigeonError,
    Ping,
    Redeem,
    Renew,
    Share,
    Take,
)


async def open_session(relay):
    """Return (owner token, joined conversation) for a session the peer joined."""
    token = secrets.token_urlsafe(32)
    async with open_client(relay.url, relay.alice) as owner:
        shared = await owner.call(Share(session_id=uuid4(), session_token=token))
    async with open_client(relay.url, relay.bob) as peer:
        joined = await peer.call(Redeem(key=shared["key"], peer_session=True))
    return token, shared["address"], UUID(joined["conversation_id"])


@pytest.mark.parametrize(
    ("method", "frame"),
    [
        ("inspect", lambda ids: Inspect(conversation_id=ids["conversation"])),
        ("list_calls", lambda ids: ListCalls()),
        ("history", lambda ids: History(conversation_id=ids["conversation"])),
        ("authenticate", lambda ids: Ping()),
        ("renew", lambda ids: Renew(message_id=ids["request"])),
        (
            "finish",
            lambda ids: Finish(message_id=ids["request"], status="completed", content="Done"),
        ),
        ("take", lambda ids: Take(wait=0, claim_id=ids["claim"])),
    ],
)
def test_transient_storage_errors_are_retried(faulty_relay, method, frame):
    relay, faults = faulty_relay

    async def exchange():
        token, inbox, conversation = await open_session(relay)
        async with open_client(relay.url, relay.bob) as peer:
            asked = await peer.ask(inbox, "Question", conversation_id=conversation, wait=0)
        ids = {"conversation": conversation, "request": asked.request.id, "claim": uuid4()}
        async with open_client(relay.url, token) as inbox_client:
            if method != "take":
                await inbox_client.call(Take(wait=0))
            faults.fail(method, times=2)
            result = await inbox_client.call(frame(ids))
        assert faults.calls[method] >= 3
        return result, asked

    result, asked = asyncio.run(exchange())
    if method == "take":
        assert result["request"]["id"] == str(asked.request.id)


def test_exhausted_retries_report_the_storage_error(faulty_relay):
    relay, faults = faulty_relay

    async def exchange():
        async with open_client(relay.url, relay.alice) as client:
            faults.fail("list_calls", times=10)
            with pytest.raises(PigeonError) as caught:
                await client.call(ListCalls())
            assert caught.value.code == "storage_unavailable"
            assert faults.calls["list_calls"] == len(STORAGE_RETRIES) + 1

    asyncio.run(exchange())


def test_errors_that_are_not_transient_are_not_retried(faulty_relay):
    relay, faults = faulty_relay

    async def exchange():
        token, inbox, conversation = await open_session(relay)
        async with open_client(relay.url, relay.bob) as peer:
            asked = await peer.ask(inbox, "Question", conversation_id=conversation, wait=0)
            faults.fail("inspect", times=0)
            # The sender cannot finish its own request. Authorization errors stay final.
            with pytest.raises(PigeonError) as caught:
                await peer.call(
                    Finish(message_id=asked.request.id, status="completed", content="x")
                )
            assert caught.value.code == "not_found"
            with pytest.raises(PigeonError) as caught:
                await peer.call(Inspect(conversation_id=uuid4()))
            assert caught.value.code == "not_found"
            assert faults.calls["inspect"] == 1

    asyncio.run(exchange())


def test_a_claim_that_committed_before_the_error_is_returned_once(faulty_relay):
    relay, faults = faulty_relay

    async def exchange():
        token, inbox, conversation = await open_session(relay)
        async with open_client(relay.url, relay.bob) as peer:
            first = await peer.ask(inbox, "First", conversation_id=conversation, wait=0)
        async with open_client(relay.url, token) as inbox_client:
            # Without a claim ID, the relay cannot tell a retry from a new claim.
            faults.fail("take", after_commit=True)
            with pytest.raises(PigeonError):
                await inbox_client.call(Take(wait=0))
            assert faults.calls["take"] == 1
            stranded = await inbox_client.call(Inspect(conversation_id=conversation))
            assert stranded["status"] == "running"
            await inbox_client.call(
                Finish(message_id=first.request.id, status="completed", content="Late")
            )

            async with open_client(relay.url, relay.bob) as peer:
                second = await peer.ask(inbox, "Second", conversation_id=conversation, wait=0)
            claim_id = uuid4()
            faults.fail("take", after_commit=True)
            claimed = await inbox_client.take(0, claim_id)
            assert claimed.request.id == second.request.id
            # Asking again with the same claim ID returns that claim, never a second one.
            assert (await inbox_client.take(0, claim_id)).request.id == second.request.id
            assert await inbox_client.take(0, uuid4()) is None

    asyncio.run(exchange())


def test_retries_stop_at_the_callers_deadline(faulty_relay):
    relay, faults = faulty_relay

    async def exchange():
        async with open_client(relay.url, relay.alice) as client:
            faults.fail("list_calls", times=10)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(PigeonError):
                await client.call(ListCalls(), until=started + 0.1)
            assert loop.time() - started < 0.5

    asyncio.run(exchange())


class Counting(Client):
    waits: list[int] = []

    async def send(self, request):
        if isinstance(request, Inspect):
            Counting.waits.append(request.wait)
        return await super().send(request)


@pytest.mark.parametrize("finish", ["completed", "failed", None])
def test_status_wait_returns_the_final_state(relay, monkeypatch, finish):
    # Smaller relay waits make a multi-step wait fast to test.
    monkeypatch.setattr("pigeon.client.MAX_WAIT", 2)
    monkeypatch.setattr("pigeon.client.Client", Counting)
    Counting.waits = []

    async def exchange():
        token, inbox, conversation = await open_session(relay)
        async with open_client(relay.url, relay.bob) as peer:
            asked = await peer.ask(inbox, "Question", conversation_id=conversation, wait=0)

        async def recipient():
            async with open_client(relay.url, token) as client:
                await asyncio.sleep(1)
                await client.call(Take(wait=0))
                if finish:
                    await asyncio.sleep(2.5)
                    await client.call(
                        Finish(message_id=asked.request.id, status=finish, content="Result")
                    )

        work = asyncio.create_task(recipient())
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with open_client(relay.url, relay.bob) as peer:
            turn = await peer.status(conversation, asked.request.id, wait=6)
        await work
        return turn, loop.time() - started

    turn, elapsed = asyncio.run(exchange())
    if finish:
        # One wait spans the claim and returns the answer.
        assert turn.status == finish and turn.response.content == "Result"
        assert 3 < elapsed < 5.5
    # Every relay call long-polls. None is a bare status check before waiting.
    assert Counting.waits and all(wait > 0 for wait in Counting.waits)
    assert len(Counting.waits) <= 4
    if not finish:
        # A claimed request still running is returned when the wait ends, not at the claim.
        assert turn.status == "running"
        assert 5.5 < elapsed < 8


def test_status_after_returns_on_the_transition_and_wait_zero_is_immediate(relay):
    async def exchange():
        token, inbox, conversation = await open_session(relay)
        async with open_client(relay.url, relay.bob) as peer:
            asked = await peer.ask(inbox, "Question", conversation_id=conversation, wait=0)
            now = await peer.status(conversation, asked.request.id)
            assert now.status == "queued"

            async def claim():
                await asyncio.sleep(1)
                async with open_client(relay.url, token) as client:
                    await client.call(Take(wait=0))

            work = asyncio.create_task(claim())
            loop = asyncio.get_running_loop()
            started = loop.time()
            moved = await peer.status(conversation, asked.request.id, wait=20, after="queued")
            assert moved.status == "running" and loop.time() - started < 5
            await work
            # Waiting on a state the request already left returns at once.
            started = loop.time()
            again = await peer.status(conversation, asked.request.id, wait=20, after="queued")
            assert again.status == "running" and loop.time() - started < 1

    asyncio.run(exchange())


def test_old_relays_still_receive_without_claim_ids():
    import json

    from websockets.asyncio.server import serve

    frames = []

    async def old_relay(socket):
        async for raw in socket:
            frame = json.loads(raw)
            frames.append(frame)
            # A relay before claim IDs rejects unknown fields as invalid frames.
            if "claim_id" in frame or None in frame.values():
                reply = {"error": "invalid_frame", "message": "Invalid protocol frame."}
            else:
                reply = {"ok": None}
            await socket.send(json.dumps(reply))

    async def exchange():
        async with serve(old_relay, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with open_client(f"ws://127.0.0.1:{port}", "x" * 43) as client:
                assert await client.receive(0, uuid4()) is None
                assert await client.call(Inspect(conversation_id=uuid4())) is None

    asyncio.run(exchange())
    assert [sorted(frame) for frame in frames] == [
        ["claim_id", "op", "wait"],
        ["op", "wait"],
        ["conversation_id", "op", "wait"],
    ]
