import asyncio
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from pigeon.attachments import download_file, transfer, upload_file
from pigeon.client import open_client
from pigeon.config import Config
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
