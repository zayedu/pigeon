import asyncio
import secrets
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from conftest import ALICE, BOB, relay_process
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

from pigeon.client import open_client, relay_endpoint
from pigeon.models import (
    MAX_FRAME,
    Context,
    Finish,
    Grant,
    Inspect,
    PigeonError,
    Ping,
    Redeem,
    Share,
    Submit,
    Take,
    Watch,
    command,
)


def test_protocol_validation():
    frame = Submit(
        to=ALICE,
        message_id=uuid4(),
        conversation_id=uuid4(),
        message="Why cache misses?",
        context=Context(type="github_pr", repo="alice/project", number=42),
    )
    assert command.validate_json(frame.model_dump_json()) == frame
    assert Context(type="github_pr", repo="alice/.github", number=42).repo == "alice/.github"
    data = frame.model_dump()
    for changes in (
        {"sender": BOB},
        {"message_id": "abc"},
        {"conversation_id": "abc"},
        {"to": "https://example.org"},
        {"message": ""},
        {"message": "x" * 4001},
        {"context": {"type": "github_pr", "repo": "../project", "number": 42}},
        {"context": {"type": "github_pr", "repo": "alice/..", "number": 42}},
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


def test_relay_restart_preserves_queue_without_replaying_claimed_work(identities):
    store, alice, bob = identities

    async def before_restart(relay):
        async with open_client(relay, alice) as receiver, open_client(relay, bob) as caller:
            await receiver.call(Grant(op="allow", sender=BOB))
            active = await caller.ask(ALICE, "Claimed before restart", wait=0)
            assert (await receiver.call(Take(wait=0)))["request"]["id"] == str(active.request.id)
            queued = await caller.ask(ALICE, "Queued before restart", wait=0)
            return active, queued

    async def after_restart(relay, active, queued):
        async with open_client(relay, alice) as receiver, open_client(relay, bob) as caller:
            assert (
                await caller.turn(Inspect(conversation_id=active.request.conversation_id))
            ).status == "running"
            assert (await receiver.call(Take(wait=0)))["request"]["id"] == str(queued.request.id)
            assert await receiver.call(Take(wait=0)) is None
            saved = await receiver.call(
                Finish(message_id=active.request.id, status="completed", content="Recovered result")
            )
            assert saved == await receiver.call(
                Finish(message_id=active.request.id, status="completed", content="Recovered result")
            )
            assert (
                await caller.turn(Inspect(conversation_id=active.request.conversation_id))
            ).response.content == "Recovered result"

    with relay_process(store) as url:
        active, queued = asyncio.run(before_restart(url))
    with relay_process(store) as url:
        asyncio.run(after_restart(url, active, queued))


def test_peer_revocation_and_conversation_privacy(relay):
    async def exchange():
        third = relay.store.enroll("@charlie/agent")
        async with (
            open_client(relay.url, relay.alice) as receiver,
            open_client(relay.url, relay.bob) as caller,
            open_client(relay.url, third) as outsider,
        ):
            await receiver.call(Grant(op="allow", sender=BOB))
            turn = await caller.ask(ALICE, "Private request", wait=0)
            query = Inspect(conversation_id=turn.request.conversation_id)
            with pytest.raises(PigeonError) as exc:
                await outsider.turn(query)
            assert exc.value.code == "not_found"
            assert (await receiver.call(Take(wait=0)))["status"] == "running"
            await receiver.call(Grant(op="deny", sender=BOB))
            assert (await caller.turn(query)).status == "cancelled"
            with pytest.raises(PigeonError) as exc:
                await caller.ask(ALICE, "After deny", wait=0)
            assert exc.value.code == "forbidden"
            relay.store.revoke(BOB)
            with pytest.raises(PigeonError) as exc:
                await caller.call(Ping())
            assert exc.value.code == "unauthenticated"

    asyncio.run(exchange())


def test_shared_store_preserves_transactions(monkeypatch, tmp_path):
    import libsql

    from pigeon.db import RemoteStore, token_hash

    connect = libsql.connect

    def local_shared_database(url, **kwargs):
        assert url == "libsql://relay.example.test"
        return connect(str(tmp_path / "shared.sqlite3"), **kwargs)

    monkeypatch.setattr(libsql, "connect", local_shared_database)
    sender = RemoteStore("libsql://relay.example.test", "test-token")
    recipient = RemoteStore("libsql://relay.example.test", "test-token")
    alice = sender.enroll(ALICE)
    recipient.enroll(BOB)
    assert recipient.authenticate(token_hash(alice)) == ALICE
    recipient.grant(BOB, ALICE, True)
    request = Submit(to=BOB, conversation_id=uuid4(), message_id=uuid4(), message="Review this.")
    sender.submit(ALICE, request)
    work = recipient.take(BOB)
    assert work["request"]["id"] == str(request.message_id)
    assert sender.take(BOB) is None
    recipient.finish(
        BOB, Finish(message_id=request.message_id, status="completed", content="Done.")
    )
    assert sender.inspect(ALICE, Inspect(conversation_id=request.conversation_id))["status"] == (
        "completed"
    )


def test_peers_alternate_after_the_invitation_expires(relay):
    async def exchange():
        token = secrets.token_urlsafe(32)
        async with open_client(relay.url, relay.alice) as owner:
            shared = await owner.call(Share(session_id=uuid4(), session_token=token))
        async with open_client(relay.url, relay.bob) as joiner:
            joined = await joiner.call(Redeem(key=shared["key"]))
            conversation_id = joined["conversation_id"]
            first = await joiner.turn(
                Submit(
                    to=joined["address"],
                    conversation_id=conversation_id,
                    message_id=uuid4(),
                    message="Can you see this?",
                )
            )
        async with open_client(relay.url, token) as inbox:
            claimed = await inbox.call(Take(wait=0))
            assert claimed["request"]["content"] == "Can you see this?"
            await inbox.call(
                Finish(message_id=first.request.id, status="completed", content="Yes.")
            )
            reverse = await inbox.turn(
                Submit(
                    to=BOB,
                    conversation_id=conversation_id,
                    message_id=uuid4(),
                    message="What changed?",
                )
            )
        async with open_client(relay.url, relay.bob) as joiner:
            incoming = await joiner.call(Take(wait=0))
            assert incoming["request"]["id"] == str(reverse.request.id)
            await joiner.call(
                Finish(message_id=reverse.request.id, status="completed", content="The index.")
            )
            again = await joiner.turn(
                Submit(
                    to=joined["address"],
                    conversation_id=conversation_id,
                    message_id=uuid4(),
                    message="And after that?",
                )
            )
        with sqlite3.connect(relay.store.path) as db:
            db.execute("UPDATE session_shares SET expires_at=0")
            db.execute("UPDATE chat_invites SET expires_at=0")
        async with open_client(relay.url, token) as inbox:
            assert (await inbox.call(Take(wait=0)))["request"]["id"] == str(again.request.id)
            await inbox.call(
                Finish(message_id=again.request.id, status="completed", content="Still here.")
            )
            continued = await inbox.turn(
                Submit(
                    to=BOB,
                    conversation_id=conversation_id,
                    message_id=uuid4(),
                    message="Membership outlives the key.",
                )
            )
            assert continued.request.sender == joined["address"]
        async with open_client(relay.url, relay.bob) as joiner:
            with pytest.raises(PigeonError) as exc:
                await joiner.call(Redeem(key=shared["key"]))
            assert exc.value.code == "invalid_key"
            delivered = await joiner.call(Take(wait=0))
            assert delivered["request"]["content"] == "Membership outlives the key."
            assert delivered["status"] == "running"

    asyncio.run(exchange())


def test_one_invitation_accepts_one_joiner(relay):
    async def exchange():
        charlie = relay.store.enroll("@charlie/agent")
        async with open_client(relay.url, relay.alice) as owner:
            shared = await owner.call(
                Share(session_id=uuid4(), session_token=secrets.token_urlsafe(32))
            )

        async def redeem(token):
            try:
                async with open_client(relay.url, token) as client:
                    return await client.call(Redeem(key=shared["key"]))
            except PigeonError as exc:
                return exc

        first, second = await asyncio.gather(redeem(relay.bob), redeem(charlie))
        results = {type(item) for item in (first, second)}
        assert results == {dict, PigeonError}
        error = first if isinstance(first, PigeonError) else second
        assert error.code == "invalid_key"

    asyncio.run(exchange())


def test_simultaneous_follow_ups_do_not_both_land(relay):
    async def exchange():
        async with open_client(relay.url, relay.alice) as receiver:
            await receiver.call(Grant(op="allow", sender=BOB))
        conversation_id = uuid4()

        async def send(text):
            try:
                async with open_client(relay.url, relay.bob) as caller:
                    return await caller.turn(
                        Submit(
                            to=ALICE,
                            conversation_id=conversation_id,
                            message_id=uuid4(),
                            message=text,
                        )
                    )
            except PigeonError as exc:
                return exc

        results = await asyncio.gather(send("One"), send("Two"))
        assert sum(isinstance(item, PigeonError) for item in results) == 1
        assert any(getattr(item, "code", None) == "conflict" for item in results)

    asyncio.run(exchange())


def test_legacy_turn_direction_survives_adding_columns(tmp_path):
    from pigeon.db import Store

    path = tmp_path / "legacy.sqlite3"
    store = Store(path)
    store.enroll(ALICE)
    store.enroll(BOB)
    store.grant(BOB, ALICE, True)
    conversation_id = str(uuid4())
    message_id = str(uuid4())
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, NULL)",
            (conversation_id, ALICE, BOB, 1),
        )
        db.execute(
            """INSERT INTO turns
               (id, conversation_id, request, timeout, created_at, deadline, status)
               VALUES (?, ?, ?, 30, 1, 9999999999, 'queued')""",
            (message_id, conversation_id, "Legacy question"),
        )
    again = Store(path)
    turn = again.inspect(ALICE, Inspect(conversation_id=conversation_id))
    assert turn["request"]["sender"] == ALICE
    assert turn["request"]["recipient"] == BOB
    assert turn["status"] == "queued"


def test_pre_change_database_keeps_an_inflight_request(tmp_path):
    from pigeon.db import MIGRATIONS, Store

    path = tmp_path / "before.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE agents (address TEXT PRIMARY KEY, token_hash TEXT UNIQUE);
            CREATE TABLE grants (
                recipient TEXT NOT NULL, sender TEXT NOT NULL, PRIMARY KEY (recipient, sender)
            );
            CREATE TABLE signup_limits (source TEXT PRIMARY KEY, day INTEGER, count INTEGER);
            CREATE TABLE session_shares (
                recipient TEXT PRIMARY KEY, owner TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                expires_at REAL NOT NULL, reviewer TEXT
            );
            CREATE TABLE shared_chats (
                owner TEXT NOT NULL, id TEXT NOT NULL, recipient TEXT NOT NULL UNIQUE,
                PRIMARY KEY (owner, id)
            );
            CREATE TABLE chat_invites (
                key_hash TEXT PRIMARY KEY, recipient TEXT NOT NULL,
                expires_at REAL NOT NULL, reviewer TEXT
            );
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                created_at REAL NOT NULL, context TEXT
            );
            CREATE TABLE turns (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, request TEXT NOT NULL,
                submitted_context TEXT, timeout INTEGER NOT NULL, created_at REAL NOT NULL,
                deadline REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                response_id TEXT, response TEXT, responded_at REAL, detail TEXT
            );
            """
        )
        db.execute("INSERT INTO agents VALUES (?, ?), (?, ?)", (ALICE, "a", BOB, "b"))
        conversation_id = str(uuid4())
        message_id = str(uuid4())
        db.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, 10, NULL)",
            (
                conversation_id,
                ALICE,
                BOB,
            ),
        )
        db.execute(
            """INSERT INTO turns
               (id, conversation_id, request, timeout, created_at, deadline, status)
               VALUES (?, ?, 'Still waiting', 30, 10, 9999999999, 'running')""",
            (message_id, conversation_id),
        )
    path.chmod(0o600)
    store = Store(path)
    Store(path)
    turn = store.inspect(BOB, Inspect(conversation_id=conversation_id, message_id=message_id))
    assert turn["status"] == "running"
    assert turn["request"]["sender"] == ALICE
    assert turn["request"]["content"] == "Still waiting"
    import libsql

    connection = libsql.connect(str(tmp_path / "libsql.db"))
    try:
        connection.execute(
            "CREATE TABLE turns (id TEXT PRIMARY KEY, conversation_id TEXT, status TEXT)"
        )
        connection.execute("CREATE TABLE chat_invites (key_hash TEXT PRIMARY KEY)")
        for _ in range(2):
            for statement in MIGRATIONS:
                try:
                    connection.execute(statement)
                except Exception as exc:
                    message = str(exc).lower()
                    if "duplicate column" not in message and "already exists" not in message:
                        raise
        columns = {row[1] for row in connection.execute("PRAGMA table_info(turns)").fetchall()}
    finally:
        connection.close()
    assert {"sender", "recipient"} <= columns


def test_queued_work_is_not_hidden_by_a_claimed_request(relay):
    async def exchange():
        token = secrets.token_urlsafe(32)
        session_id = uuid4()
        async with open_client(relay.url, relay.alice) as owner:
            shared = await owner.call(Share(session_id=session_id, session_token=token))
        async with open_client(relay.url, relay.bob) as joiner:
            joined = await joiner.call(Redeem(key=shared["key"]))
            first = await joiner.turn(
                Submit(
                    to=joined["address"],
                    conversation_id=joined["conversation_id"],
                    message_id=uuid4(),
                    message="Claimed first",
                )
            )
        async with open_client(relay.url, token) as inbox:
            claimed = await inbox.call(Take(wait=0))
            assert claimed["request"]["id"] == str(first.request.id)
            assert await inbox.call(Watch(wait=0)) is None
        async with open_client(relay.url, relay.alice) as owner:
            second_key = (await owner.call(Share(session_id=session_id, session_token=token)))[
                "key"
            ]
        async with open_client(relay.url, relay.bob) as joiner:
            other = await joiner.call(Redeem(key=second_key))
            await joiner.turn(
                Submit(
                    to=other["address"],
                    conversation_id=other["conversation_id"],
                    message_id=uuid4(),
                    message="Queued second",
                )
            )
        async with open_client(relay.url, token) as inbox:
            noticed = await inbox.call(Watch(wait=0))
            assert noticed["message_id"] != str(first.request.id)
            delivered = await inbox.call(Take(wait=0))
            assert delivered["request"]["content"] == "Queued second"
            assert (await inbox.call(Take(wait=0)))["request"]["content"] == "Claimed first"

    asyncio.run(exchange())


def test_revoking_a_grant_leaves_another_session_running(relay):
    async def exchange():
        first_token = secrets.token_urlsafe(32)
        second_token = secrets.token_urlsafe(32)
        async with open_client(relay.url, relay.alice) as owner:
            first = await owner.call(Share(session_id=uuid4(), session_token=first_token))
            second = await owner.call(Share(session_id=uuid4(), session_token=second_token))
            await owner.call(Grant(op="allow", sender=BOB))
        async with open_client(relay.url, relay.bob) as joiner:
            one = await joiner.call(Redeem(key=first["key"]))
            two = await joiner.call(Redeem(key=second["key"]))
            await joiner.turn(
                Submit(
                    to=one["address"],
                    conversation_id=one["conversation_id"],
                    message_id=uuid4(),
                    message="Session one",
                )
            )
            await joiner.turn(
                Submit(
                    to=two["address"],
                    conversation_id=two["conversation_id"],
                    message_id=uuid4(),
                    message="Session two",
                )
            )
            legacy = await joiner.turn(
                Submit(to=ALICE, conversation_id=uuid4(), message_id=uuid4(), message="Legacy")
            )
        async with open_client(relay.url, relay.alice) as owner:
            await owner.call(Grant(op="deny", sender=BOB))
            assert (
                await owner.turn(Inspect(conversation_id=legacy.request.conversation_id))
            ).status == "cancelled"
        async with open_client(relay.url, first_token) as inbox:
            assert (
                await inbox.turn(Inspect(conversation_id=one["conversation_id"]))
            ).status == "queued"
        async with open_client(relay.url, second_token) as inbox:
            assert (
                await inbox.turn(Inspect(conversation_id=two["conversation_id"]))
            ).status == "queued"

    asyncio.run(exchange())
