import asyncio
import json
import os
import pty
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from pigeon.attachments import download_file, transfer, upload_file
from pigeon.client import open_client
from pigeon.config import Config, chat_share, save
from pigeon.models import MAX_ATTACHMENT, Capabilities, Close, PigeonError, Redeem, Share


def test_attachment_round_trip_is_scoped_to_an_open_session(relay, tmp_path):
    async def connect():
        async with open_client(relay.url, relay.alice) as owner:
            shared = await owner.call(Share(session_id=uuid4(), session_token="s" * 43))
        async with open_client(relay.url, relay.bob) as bob:
            joined = await bob.call(Redeem(key=shared["key"], peer_session=True))
        owner = Config(relay=relay.url, agent=shared["address"], token=shared["token"])
        peer = Config(relay=relay.url, agent="@bob/claude", token=relay.bob)
        outsider = Config(relay=relay.url, agent="@alice/codex", token=relay.alice)
        return UUID(joined["conversation_id"]), owner, peer, outsider

    conversation, owner, peer, outsider = asyncio.run(connect())
    source = tmp_path / "diagram.png"
    data = b"\x89PNG\r\n\x1a\n\x00\xff"
    source.write_bytes(data)
    with pytest.raises(PigeonError, match="Peer attachment support"):
        upload_file(owner, conversation, source)

    async def advertise():
        async with open_client(relay.url, peer.token) as client:
            await client.call(Capabilities(features=["attachments", "manual_inbox", "wake"]))
        with relay.store.transaction() as db:
            db.execute("UPDATE chat_capabilities SET expires_at=0 WHERE agent=?", (peer.agent,))
        async with open_client(relay.url, owner.token) as client:
            result = await client.call(Capabilities(conversation_id=conversation))
        assert result["peer"] == peer.agent
        assert "attachments" in result["features"]
        assert "wake" not in result["features"]

    asyncio.run(advertise())
    with pytest.raises(PigeonError, match="exceeds"):
        transfer(
            owner,
            f"/v1/sessions/{conversation}/attachments?filename=big.txt",
            data=b"x" * (MAX_ATTACHMENT + 1),
        )
    record = upload_file(owner, conversation, source)
    attachment = UUID(record["id"])
    target = tmp_path / "received.png"
    assert record["sha256"] == sha256(data).hexdigest()
    assert record["reference"] == f"pigeon-attachment:{attachment}"
    with pytest.raises(PigeonError, match="not found"):
        download_file(outsider, conversation, attachment, target)
    assert not target.exists()
    download_file(peer, conversation, attachment, target)
    assert target.read_bytes() == data
    with pytest.raises(PigeonError, match="does not exist"):
        download_file(peer, conversation, attachment, target)

    with relay.store.transaction() as db:
        db.execute("UPDATE attachments SET data=? WHERE id=?", (b"changed", str(attachment)))
    with pytest.raises(PigeonError, match="integrity"):
        download_file(peer, conversation, attachment, tmp_path / "corrupt.png")
    assert not (tmp_path / "corrupt.png").exists()
    with relay.store.transaction() as db:
        db.execute("UPDATE attachments SET expires_at=0 WHERE id=?", (str(attachment),))
    with pytest.raises(PigeonError, match="not found"):
        download_file(peer, conversation, attachment, tmp_path / "expired.png")
    relay.store.prune_attachments()
    with relay.store.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM attachment_uploaders").fetchone()[0] == 0

    live = upload_file(owner, conversation, source)

    async def close():
        async with open_client(relay.url, owner.token) as client:
            await client.call(Close(conversation_id=conversation))

    asyncio.run(close())
    with pytest.raises(PigeonError, match="not found"):
        download_file(peer, conversation, UUID(live["id"]), tmp_path / "closed.png")


def test_libsql_attachment_bytes_are_preserved(tmp_path, monkeypatch):
    import libsql

    from pigeon.db import RemoteStore

    connect = libsql.connect
    monkeypatch.setattr(
        libsql, "connect", lambda _url, **kw: connect(str(tmp_path / "shared.sqlite3"), **kw)
    )
    store = RemoteStore("libsql://test", "local-fixture")
    store.enroll("@alice/agent")
    store.enroll("@bob/agent")
    shared = store.share("@alice/agent", uuid4(), "x" * 43)
    session = store.redeem("@bob/agent", shared["key"], True)["conversation_id"]
    store.capabilities("@bob/agent", None, ["attachments"])
    data = bytes(range(256))
    record = store.upload_attachment(shared["address"], session, "example.pdf", data)
    downloaded = store.download_attachment("@bob/agent", session, record["id"])
    assert bytes(downloaded["data"]) == data
    assert downloaded["digest"] == record["sha256"]


def test_same_account_byte_quota_is_atomic_across_chats(identities, monkeypatch):
    store, _, _ = identities
    monkeypatch.setattr("pigeon.db.MAX_ATTACHMENT", 16)
    store.capabilities("@bob/claude", None, ["attachments"])

    def session():
        shared = store.share("@alice/codex", uuid4(), "x" * 42 + str(len(inboxes)))
        inboxes.append(shared["address"])
        joined = store.redeem("@bob/claude", shared["key"], True)
        return shared["address"], joined["conversation_id"]

    inboxes = []
    for count in (8, 7):
        actor, conversation = session()
        for _ in range(count):
            store.upload_attachment(actor, conversation, "first.txt", b"x" * 16)
    pairs = [session(), session()]

    def upload(pair):
        try:
            store.upload_attachment(*pair, "concurrent.txt", b"x" * 16)
        except PigeonError as exc:
            assert exc.code == "limit" and "account" in str(exc)
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(upload, pairs)) == [False, True]
    # A separate uploader account still has capacity in the same session.
    actor, conversation = pairs[0]
    store.capabilities(actor, None, ["attachments"])
    store.upload_attachment("@bob/claude", conversation, "reply.txt", b"x" * 16)


def test_attachment_cli_refuses_piped_confirmation(tmp_path):
    source = tmp_path / "private.txt"
    source.write_text("test secret")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pigeon.cli",
            "attachment",
            "upload",
            str(uuid4()),
            str(source),
            "--chat",
            "codex:example",
        ],
        input="y\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "interactive terminal" in result.stderr
    assert "test secret" not in result.stdout + result.stderr


def run_approved_attachment(profile, *args):
    master, terminal = pty.openpty()
    try:
        with subprocess.Popen(
            [sys.executable, "-m", "pigeon.cli", "--config", str(profile), "attachment", *args],
            stdin=terminal,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            os.write(master, b"y\n")
            try:
                output, error = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            assert process.returncode == 0, error
    finally:
        os.close(master)
        os.close(terminal)
    assert "Authorize this transfer" in output
    return json.loads(output[output.index("{") :])


def test_attachment_cli_confirms_round_trip(relay, tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("approved")
    cfg = Config(relay=relay.url, agent="@alice/codex", token=relay.alice)
    profile = tmp_path / "alice.json"
    save(profile, cfg)
    chat = "codex:fixture"
    identity = chat_share(profile, cfg, chat)

    async def invite():
        async with open_client(relay.url, relay.alice) as owner:
            shared = await owner.call(identity)
        async with open_client(relay.url, shared["token"]) as chat_client:
            await chat_client.call(Capabilities(features=["attachments"]))
        async with open_client(relay.url, relay.bob) as peer:
            joined = await peer.call(Redeem(key=shared["key"], peer_session=True))
            await peer.call(Capabilities(features=["attachments"]))
        return joined["conversation_id"]

    session = asyncio.run(invite())
    record = run_approved_attachment(profile, "upload", session, str(source), "--chat", chat)
    peer = Config(relay=relay.url, agent="@bob/claude", token=relay.bob)
    destination = tmp_path / "received.txt"
    download_file(peer, UUID(session), UUID(record["id"]), destination, record["sha256"])
    assert destination.read_text() == "approved"
    reply = upload_file(peer, UUID(session), destination)
    returned = tmp_path / "returned.txt"
    run_approved_attachment(
        profile,
        "download",
        session,
        reply["id"],
        str(returned),
        "--sha256",
        reply["sha256"],
        "--chat",
        chat,
    )
    assert returned.read_bytes() == source.read_bytes()


def test_upload_refuses_symlink_and_fifo(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("The file must be rejected before transfer")

    monkeypatch.setattr("pigeon.attachments.transfer", unexpected)
    cfg = Config(relay="ws://127.0.0.1:8765", agent="@alice/codex", token="t" * 43)
    secret = tmp_path / "private.txt"
    secret.write_text("test secret")
    link = tmp_path / "link.txt"
    link.symlink_to(secret)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    for path in (link, fifo):
        with pytest.raises(PigeonError):
            upload_file(cfg, uuid4(), path)
