import asyncio
import json
import secrets
from uuid import uuid4

import pytest
from conftest import ALICE, BOB

from pigeon.client import open_client
from pigeon.db import LEASE_EXPIRED, QUEUE_EXPIRED, Store
from pigeon.models import (
    LEASE,
    MAX_FRAME,
    MAX_RUN,
    MAX_TEXT_BYTES,
    Capabilities,
    Finish,
    History,
    Inspect,
    PigeonError,
    Redeem,
    Renew,
    Share,
    Submit,
    Take,
    Turn,
    message_problem,
)


class Clock:
    def __init__(self, monkeypatch):
        self.now = 1_000_000.0
        monkeypatch.setattr("pigeon.db.time.time", lambda: self.now)


@pytest.fixture
def session(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    store = Store(tmp_path / "relay.sqlite3")
    store.enroll(ALICE)
    store.enroll(BOB)
    shared = store.share(ALICE, uuid4(), secrets.token_urlsafe(32))
    joined = store.redeem(BOB, shared["key"], peer_session=True)
    return store, clock, shared["address"], joined["conversation_id"]


def ask(store, sender, to, conversation_id, text="Question", **fields):
    return store.submit(
        sender,
        Submit(
            to=to,
            conversation_id=conversation_id,
            message_id=fields.pop("message_id", uuid4()),
            message=text,
            **fields,
        ),
    )


def test_claim_starts_a_bounded_renewable_execution_lease(session):
    store, clock, inbox, conversation_id = session
    turn = ask(store, BOB, inbox, conversation_id, timeout=300)
    assert turn["deadline"] == clock.now + 300

    clock.now += 290
    claimed = store.take(inbox)
    claimed_at = clock.now
    assert claimed["status"] == "running"
    assert claimed["deadline"] == claimed_at + LEASE
    message_id = claimed["request"]["id"]

    # Past the original queue deadline, the claimed turn keeps running.
    clock.now += 300
    assert not store.expire()

    clock.now += 200
    renewed = store.renew(inbox, Renew(message_id=message_id))
    assert renewed["deadline"] == clock.now + LEASE
    with pytest.raises(PigeonError) as caught:
        store.renew(BOB, Renew(message_id=message_id))
    assert caught.value.code == "not_found"

    while clock.now + LEASE < claimed_at + MAX_RUN:
        clock.now += LEASE - 1
        store.renew(inbox, Renew(message_id=message_id))
    capped = store.renew(inbox, Renew(message_id=message_id))
    assert capped["deadline"] == claimed_at + MAX_RUN

    # Finishing exactly at the deadline is too late, and renewal cannot revive it.
    clock.now = capped["deadline"]
    late = store.finish(inbox, Finish(message_id=message_id, status="completed", content="x"))
    assert (late["status"], late["detail"], late["response"]) == (
        "timed_out",
        LEASE_EXPIRED,
        None,
    )
    clock.now += 1
    assert store.renew(inbox, Renew(message_id=message_id))["status"] == "timed_out"


def test_queue_deadline_and_finishing_just_before_the_lease_ends(session):
    store, clock, inbox, conversation_id = session
    expired = ask(store, BOB, inbox, conversation_id, timeout=5)
    clock.now += 5
    assert store.expire()
    turn = store.inspect(BOB, Inspect(conversation_id=conversation_id))
    assert (turn["request"]["id"], turn["status"], turn["detail"]) == (
        expired["request"]["id"],
        "timed_out",
        QUEUE_EXPIRED,
    )

    ask(store, BOB, inbox, conversation_id)
    claimed = store.take(inbox)
    assert store.renew(inbox, Renew(message_id=expired["request"]["id"]))["status"] == "timed_out"
    clock.now = claimed["deadline"] - 0.001
    done = store.finish(
        inbox, Finish(message_id=claimed["request"]["id"], status="completed", content="On time")
    )
    assert done["status"] == "completed"
    assert done["response"]["content"] == "On time"


def test_collisions_report_direction_and_retries_return_the_original(session):
    store, _clock, inbox, conversation_id = session
    message_id = uuid4()
    first = ask(store, BOB, inbox, conversation_id, message_id=message_id)
    assert ask(store, BOB, inbox, conversation_id, message_id=message_id) == first

    with pytest.raises(PigeonError) as caught:
        ask(store, inbox, BOB, conversation_id, "Simultaneous")
    assert caught.value.code == "turn_active"
    assert caught.value.data == {
        "direction": "incoming",
        "conversation_id": conversation_id,
        "message_id": str(message_id),
        "status": "queued",
        "deadline": first["deadline"],
    }
    assert "Receive and answer it" in str(caught.value)

    with pytest.raises(PigeonError) as caught:
        ask(store, BOB, inbox, conversation_id, "Another")
    assert caught.value.data["direction"] == "outgoing"
    assert str(message_id) in str(caught.value)

    store.take(inbox)
    with pytest.raises(PigeonError) as caught:
        ask(store, inbox, BOB, conversation_id, "Still answering")
    assert caught.value.data["status"] == "running"
    assert "already claimed" in str(caught.value)

    store.finish(inbox, Finish(message_id=message_id, status="completed", content="Answer"))
    follow_up = ask(store, inbox, BOB, conversation_id, "Now my question")
    assert follow_up["status"] == "queued" and follow_up["follow_up"]


def test_long_messages_require_the_receiving_chat_to_accept_them(session):
    store, _clock, inbox, conversation_id = session
    long_text = "λ" * 5_000
    with pytest.raises(PigeonError) as caught:
        ask(store, BOB, inbox, conversation_id, long_text)
    assert caught.value.code == "message_limit"
    assert caught.value.data == {"characters": 5_000, "limit": 4_000}

    store.capabilities(inbox, None, ["long_messages"])
    turn = ask(store, BOB, inbox, conversation_id, long_text)
    store.take(inbox)
    finish = Finish(message_id=turn["request"]["id"], status="completed", content=long_text)
    with pytest.raises(PigeonError) as caught:
        store.finish(inbox, finish)
    assert caught.value.code == "message_limit"
    assert store.inspect(BOB, Inspect(conversation_id=conversation_id))["status"] == "running"

    store.capabilities(BOB, None, ["long_messages"])
    assert store.finish(inbox, finish)["response"]["content"] == long_text


@pytest.mark.parametrize(
    ("text", "fits"),
    [
        ("a" * 16_000, True),
        ("a" * 16_001, False),
        ("∀ε>0 ∃δ>0: |x−a|<δ ⇒ |f(x)−L|<ε\n" * 450, True),
        ("∫₀^∞ e^{−x²} dx = √π/2 " * 700, False),
        ("日本語のテキスト" * 1_249, True),
        ("日本語のテキスト" * 1_250, False),
        ("🙂" * 7_499, True),
        ("🙂" * 7_500, False),
        ('def f(x):\n    return "\\t".join(x)  # quote\n' * 300, True),
        ("\x00" * 4_999, True),
        ("\x00" * 5_000, False),
    ],
)
def test_message_limits_count_characters_and_encoded_bytes(text, fits):
    problem = message_problem(text)
    assert (problem is None) == fits
    if not fits:
        assert f"{len(text):,} characters" in problem
        assert f"{MAX_TEXT_BYTES:,} encoded bytes" in problem


def test_history_pages_by_turn_and_frame_size(session):
    store, _clock, inbox, conversation_id = session
    store.capabilities(inbox, None, ["long_messages"])
    store.capabilities(BOB, None, ["long_messages"])
    big = "é" * 14_000
    ids = []
    for index in range(25):
        sender, recipient = (BOB, inbox) if index % 2 == 0 else (inbox, BOB)
        text = big if index in (3, 4) else f"Turn {index}"
        turn = ask(store, sender, recipient, conversation_id, text)
        store.take(recipient)
        store.finish(
            recipient,
            Finish(
                message_id=turn["request"]["id"],
                status="completed",
                content=big if index in (3, 4) else f"Answer {index}",
            ),
        )
        ids.append(turn["request"]["id"])

    pages, before = [], None
    while True:
        page = store.history(BOB, History(conversation_id=conversation_id, before=before, limit=10))
        assert len(json.dumps({"ok": page}, ensure_ascii=False).encode()) < MAX_FRAME
        pages.append([turn["request"]["id"] for turn in page["turns"]])
        before = page["before"]
        if before is None:
            break
    assert [len(page) for page in pages] == [10, 10, 1, 4]
    assert [item for page in pages for item in page] == ids[::-1]
    for turn in store.history(inbox, History(conversation_id=conversation_id))["turns"]:
        Turn.model_validate(turn)

    store.enroll("@eve/agent")
    with pytest.raises(PigeonError) as caught:
        store.history("@eve/agent", History(conversation_id=conversation_id))
    assert caught.value.code == "not_found"


def test_peers_sending_at_once_over_the_relay(relay):
    async def exchange():
        token = secrets.token_urlsafe(32)
        async with open_client(relay.url, relay.alice) as owner:
            shared = await owner.call(Share(session_id=uuid4(), session_token=token))
        async with open_client(relay.url, relay.bob) as joiner:
            joined = await joiner.call(Redeem(key=shared["key"], peer_session=True))
        conversation_id = joined["conversation_id"]

        async def send(credential, to, text):
            try:
                async with open_client(relay.url, credential) as client:
                    return await client.turn(
                        Submit(
                            to=to,
                            conversation_id=conversation_id,
                            message_id=uuid4(),
                            message=text,
                        )
                    )
            except PigeonError as exc:
                return exc

        for _ in range(5):
            results = await asyncio.gather(
                send(token, BOB, "From the creator"), send(relay.bob, shared["address"], "Joiner")
            )
            [error] = [item for item in results if isinstance(item, PigeonError)]
            [accepted] = [item for item in results if not isinstance(item, PigeonError)]
            assert error.code == "turn_active"
            assert error.data["direction"] == "incoming"
            assert error.data["message_id"] == str(accepted.request.id)
            winner = token if accepted.request.recipient == shared["address"] else relay.bob
            async with open_client(relay.url, winner) as recipient:
                claimed = await recipient.call(Take(wait=0))
                assert claimed["request"]["id"] == str(accepted.request.id)
                for credential in (token, relay.bob):
                    async with open_client(relay.url, credential) as client:
                        await client.call(Capabilities(features=["long_messages"]))
                await recipient.call(
                    Finish(message_id=accepted.request.id, status="completed", content="é" * 14_000)
                )
            async with open_client(relay.url, relay.bob) as client:
                turn = await client.turn(
                    Inspect(conversation_id=conversation_id, message_id=accepted.request.id)
                )
                assert turn.status == "completed"
                page = await client.call(History(conversation_id=conversation_id, limit=50))
                assert page["turns"][0]["request"]["id"] == str(accepted.request.id)

    asyncio.run(exchange())
