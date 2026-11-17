import asyncio
import json
import secrets
import subprocess
import sys
import time
from uuid import uuid4

from pigeon.client import open_client
from pigeon.models import Capabilities, Finish, Inspect, ListCalls, Share, Submit, Take


def pigeon(profile, *args, stdin=None, check=True):
    return subprocess.run(
        [sys.executable, "-m", "pigeon.cli", "--config", str(profile), *args],
        input=stdin,
        text=True,
        capture_output=True,
        check=check,
        timeout=30,
    )


def output(profile, *args, stdin=None):
    return json.loads(pigeon(profile, *args, stdin=stdin).stdout)


def test_chat_scoped_cli_runs_a_whole_session(relay, tmp_path):
    profile = tmp_path / "alice.json"
    pigeon(profile, "login", relay.url, "--token-stdin", stdin=relay.alice)
    chat = f"cursor:{uuid4()}"
    token = secrets.token_urlsafe(32)

    async def open_peer():
        async with open_client(relay.url, relay.bob) as owner:
            shared = await owner.call(Share(session_id=uuid4(), session_token=token))
        async with open_client(relay.url, token) as peer:
            await peer.call(Capabilities(features=["long_messages"]))
        return shared

    shared = asyncio.run(open_peer())

    missing = pigeon(profile, "receive", "--chat", f"cursor:{uuid4()}", check=False)
    assert missing.returncode == 1 and "No local Pigeon inbox" in missing.stderr

    joined = output(profile, "connect", shared["key"], "--chat", chat)
    conversation_id = joined["conversation_id"]
    assert output(profile, "receive", "--chat", chat) is None

    async def ask():
        async with open_client(relay.url, token) as peer:
            [session] = await peer.call(ListCalls())
            assert session["sender"] == joined["address"]
            return await peer.turn(
                Submit(
                    to=session["recipient"],
                    conversation_id=conversation_id,
                    message_id=uuid4(),
                    message="What does the ablation show?",
                )
            )

    question = asyncio.run(ask())
    claimed = output(profile, "receive", "--chat", chat, "--wait", "5")
    assert claimed["request"]["id"] == str(question.request.id)
    assert claimed["status"] == "running"
    extended = output(profile, "extend", str(question.request.id), "--chat", chat)
    assert extended["deadline"] >= claimed["deadline"]
    long_answer = "The ablation isolates the warmup schedule.\n" * 150
    answered = output(
        profile, "answer", str(question.request.id), "-", "--chat", chat, stdin=long_answer
    )
    assert answered["status"] == "completed"
    assert answered["response"]["content"] == long_answer.removesuffix("\n")

    follow_up = output(
        profile, "reply", conversation_id, "Can you rerun it?", "--chat", chat, "--wait", "0"
    )
    assert follow_up["status"] == "queued"

    waiting = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pigeon.cli",
            "--config",
            str(profile),
            "status",
            "--conversation-id",
            conversation_id,
            "--message-id",
            follow_up["request"]["id"],
            "--chat",
            chat,
            "--wait",
            "30",
            "--after",
            "queued",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()

    async def peer_claims_after_a_pause():
        await asyncio.sleep(2)
        async with open_client(relay.url, token) as peer:
            await peer.call(Take(wait=0))

    asyncio.run(peer_claims_after_a_pause())
    changed = json.loads(waiting.communicate(timeout=30)[0])
    assert changed["status"] == "running"
    assert 1.5 < time.monotonic() - started < 20

    async def peer_answers():
        async with open_client(relay.url, token) as peer:
            await peer.call(
                Finish(message_id=follow_up["request"]["id"], status="completed", content="Done.")
            )
            return await peer.turn(
                Inspect(conversation_id=conversation_id, message_id=follow_up["request"]["id"])
            )

    assert asyncio.run(peer_answers()).status == "completed"
    page = output(profile, "history", conversation_id, "--chat", chat, "--limit", "1")
    assert [turn["request"]["id"] for turn in page["turns"]] == [follow_up["request"]["id"]]
    rest = output(
        profile, "history", conversation_id, "--chat", chat, "--before", str(page["before"])
    )
    assert [turn["request"]["id"] for turn in rest["turns"]] == [str(question.request.id)]
    assert rest["before"] is None

    # The account itself is not a member, so it cannot read the chat's session.
    denied = pigeon(profile, "history", conversation_id, check=False)
    assert denied.returncode == 1 and "not found" in denied.stderr
    closed = output(profile, "close", conversation_id, "--chat", chat)
    assert closed == {"conversation_id": conversation_id, "closed": True}
