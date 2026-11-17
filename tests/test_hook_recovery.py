import asyncio
import json
import os
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pigeon import cursor
from pigeon.client import open_client
from pigeon.config import Config, save
from pigeon.models import (
    Capabilities,
    Finish,
    History,
    Inspect,
    PigeonError,
    Redeem,
    Submit,
)

MDM_HOOK = {"command": "/usr/local/bin/mdm-audit", "matcher": "Shell"}


@pytest.fixture
def cursor_home(tmp_path, monkeypatch):
    """A Cursor install and Pigeon profile under a disposable home directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    hooks = tmp_path / ".cursor" / "hooks.json"
    hooks.parent.mkdir()
    cursor.load_key(create=True)
    return tmp_path, hooks


def bound(chat, tool, arguments=None):
    event = {
        "hook_event_name": "preToolUse",
        "tool_name": f"MCP:{tool}",
        "conversation_id": chat,
        "tool_input": arguments or {},
    }
    return cursor.hook(event)["updated_input"]


def unbound(arguments=None):
    """A call that arrives after the hook stopped running, carrying no binding."""
    return dict(arguments or {})


def mcp_params(profile):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pigeon.cli", "--config", str(profile), "mcp", "--cursor"],
        env=dict(os.environ),
    )


async def join(relay, chat, mcp, peer):
    shared = await mcp.call_tool("pigeon_share", bound(chat, "pigeon_share"))
    assert not shared.isError, shared.content[0].text
    joined = await peer.call(Redeem(key=shared.structuredContent["result"], peer_session=True))
    # The redeemed address is the shared chat's inbox.
    return UUID(joined["conversation_id"]), joined["address"]


async def ask(peer, inbox, conversation, text):
    turn = await peer.turn(
        Submit(to=inbox, conversation_id=conversation, message_id=uuid4(), message=text)
    )
    return turn.request


def text(result):
    return result.content[0].text


def test_claimed_requests_survive_a_hook_removed_mid_execution(relay, cursor_home):
    home, hooks = cursor_home
    profile = home / "config" / "pigeon" / "config.json"
    save(profile, Config(relay=relay.url, agent="@alice/codex", token=relay.alice))
    setup_command = f"{sys.executable} -m pigeon.cli cursor-hook"
    cursor.register_hook(hooks, setup_command)
    hooks.chmod(0o644)
    first, second = str(uuid4()), str(uuid4())

    async def exchange():
        async with (
            asyncio.timeout(30),
            stdio_client(mcp_params(profile)) as streams,
            ClientSession(*streams) as mcp,
            open_client(relay.url, relay.bob) as peer,
        ):
            await mcp.initialize()
            first_session, first_inbox = await join(relay, first, mcp, peer)
            second_session, second_inbox = await join(relay, second, mcp, peer)
            question = await ask(peer, first_inbox, first_session, "First chat question")
            other = await ask(peer, second_inbox, second_session, "Second chat question")

            received = await mcp.call_tool("pigeon_receive", bound(first, "pigeon_receive"))
            claim = received.structuredContent["claim"]
            assert received.structuredContent["chat"] == f"cursor:{first}"
            assert received.structuredContent["result"]["request"]["id"] == str(question.id)
            assert claim in received.content[1].text
            received = await mcp.call_tool("pigeon_receive", bound(second, "pigeon_receive"))
            other_claim = received.structuredContent["claim"]
            assert other_claim != claim

            # A device-management tool rewrites the file and drops Pigeon's hook.
            hooks.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [MDM_HOOK]}}))
            answer = {"message_id": str(question.id), "message": "Recovered answer"}
            failed = await mcp.call_tool("pigeon_answer", unbound(answer))
            assert failed.isError and "Pigeon restored it" in text(failed)
            entries = json.loads(hooks.read_text())["hooks"]["preToolUse"]
            assert entries[0] == MDM_HOOK and entries[1] == cursor.hook_entry(setup_command)
            assert hooks.stat().st_mode & 0o777 == 0o644

            # A request ID, another chat's token, or a forged binding does not authorize.
            for attempt in (
                answer | {"claim": other_claim},
                answer | {"claim": "not-a-claim"},
                answer | {"claim": claim, "chat_binding": f"{second}.{'0' * 64}"},
            ):
                refused = await mcp.call_tool("pigeon_answer", unbound(attempt))
                assert refused.isError, attempt
            turn = await peer.turn(Inspect(conversation_id=first_session, message_id=question.id))
            assert turn.status == "running"

            extended = await mcp.call_tool(
                "pigeon_extend", unbound({"message_id": str(question.id), "claim": claim})
            )
            assert extended.structuredContent["status"] == "running"
            done = await mcp.call_tool("pigeon_answer", unbound(answer | {"claim": claim}))
            assert not done.isError, text(done)
            assert done.structuredContent["status"] == "completed"
            turn = await peer.turn(Inspect(conversation_id=first_session, message_id=question.id))
            assert turn.response.content == "Recovered answer"
            # A token is spent once its request is answered.
            reused = await mcp.call_tool("pigeon_answer", unbound(answer | {"claim": claim}))
            assert reused.isError

            # The other chat's claim is untouched and still finishes with its own token.
            done = await mcp.call_tool(
                "pigeon_answer",
                unbound({"message_id": str(other.id), "message": "Second", "claim": other_claim}),
            )
            assert not done.isError, text(done)
            assert done.structuredContent["status"] == "completed"

            # Removed again after the repair: Pigeon reports it instead of rewriting.
            hooks.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [MDM_HOOK]}}))
            before = hooks.read_bytes()
            failed = await mcp.call_tool("pigeon_receive", unbound({"wait": 0}))
            assert failed.isError and "will not rewrite it" in text(failed)
            assert hooks.read_bytes() == before
            return first_session

    asyncio.run(exchange())


def test_hook_present_but_not_running_and_concurrent_rewrites(relay, cursor_home, monkeypatch):
    home, hooks = cursor_home
    command = f"{sys.executable} -m pigeon.cli cursor-hook"
    cursor.register_hook(hooks, command)
    error = cursor.missing_binding(command, may_repair=True)
    assert error.code == "chat_unbound" and "Reload Cursor" in str(error)

    # Another tool writes the file between Pigeon's read and its replacement.
    hooks.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [MDM_HOOK]}}))
    original_entry = cursor.hook_entry

    def racing_entry(value):
        hooks.write_text(json.dumps({"version": 1, "hooks": {}, "managed": True}))
        return original_entry(value)

    monkeypatch.setattr(cursor, "hook_entry", racing_entry)
    error = cursor.missing_binding(command, may_repair=True)
    assert error.code == "hook_removed" and "changed while Pigeon" in str(error)
    assert json.loads(hooks.read_text())["managed"] is True

    hooks.write_text("{not json")
    monkeypatch.setattr(cursor, "hook_entry", original_entry)
    error = cursor.missing_binding(command, may_repair=True)
    assert error.code == "hook_removed" and "Fix the JSON" in str(error)
    assert hooks.read_text() == "{not json"
    with pytest.raises(PigeonError) as caught:
        cursor.verify(f"chat.{'0' * 64}")
    assert caught.value.code == "chat_binding_invalid"


def test_binding_is_verified_once_per_tool_call(relay, cursor_home, monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session

    from pigeon.mcp import create_mcp

    home, _hooks = cursor_home
    chat = str(uuid4())
    calls = []
    verify = cursor.verify

    def counting(binding):
        calls.append(binding)
        return verify(binding)

    monkeypatch.setattr(cursor, "verify", counting)
    server = create_mcp(
        Config(relay=relay.url, agent="@alice/codex", token=relay.alice),
        cursor_chat=True,
        profile=home / "profile.json",
    )

    async def exchange():
        async with create_connected_server_and_client_session(server._mcp_server) as mcp:
            for tool, arguments in (
                ("pigeon_share", {}),
                ("pigeon_status", {}),
                ("pigeon_receive", {"wait": 0}),
            ):
                calls.clear()
                result = await mcp.call_tool(tool, bound(chat, tool, arguments))
                assert not result.isError, result.content[0].text
                assert len(calls) == 1, tool

    asyncio.run(exchange())


def test_combined_hook_loss_contention_and_waiting(faulty_relay, cursor_home):
    relay, faults = faulty_relay
    home, hooks = cursor_home
    profile = home / "config" / "pigeon" / "config.json"
    save(profile, Config(relay=relay.url, agent="@alice/codex", token=relay.alice))
    cursor.register_hook(hooks, f"{sys.executable} -m pigeon.cli cursor-hook")
    chat, other_chat = str(uuid4()), str(uuid4())

    async def exchange():
        async with (
            asyncio.timeout(60),
            stdio_client(mcp_params(profile)) as streams,
            ClientSession(*streams) as mcp,
            open_client(relay.url, relay.bob) as peer,
        ):
            await mcp.initialize()
            await peer.call(Capabilities(features=["long_messages"]))
            # 1-2. Cursor joins a session and claims the peer's request.
            session, inbox = await join(relay, chat, mcp, peer)
            other_session, _other_inbox = await join(relay, other_chat, mcp, peer)
            question = await ask(peer, inbox, session, "Profile the slow epoch.")
            received = await mcp.call_tool("pigeon_receive", bound(chat, "pigeon_receive"))
            claim = received.structuredContent["claim"]

            # 3-4. The hook disappears mid-execution. The claim token finishes the work.
            hooks.write_text(json.dumps({"version": 1, "hooks": {}}))
            long_answer = "Epoch 3 stalls on host-to-device copies.\n" * 350
            done = await mcp.call_tool(
                "pigeon_answer",
                unbound({"message_id": str(question.id), "message": long_answer, "claim": claim}),
            )
            assert not done.isError, text(done)
            assert done.structuredContent["status"] == "completed"

            # 5. The other chat sees nothing of this session.
            empty = await mcp.call_tool(
                "pigeon_receive", bound(other_chat, "pigeon_receive", {"wait": 0})
            )
            assert empty.structuredContent == {"result": None}
            denied = await mcp.call_tool(
                "pigeon_history",
                bound(other_chat, "pigeon_history", {"conversation_id": str(session)}),
            )
            assert denied.isError

            # 6-9. Cursor asks back and waits once. Polling hits storage contention, the
            # peer claims partway through, and the single call returns the final answer.
            sent = await mcp.call_tool(
                "pigeon_reply", bound(chat, "pigeon_reply", {"message": "Retry?", "wait": 0})
            )
            follow_up = sent.structuredContent
            assert follow_up["status"] == "queued"

            async def peer_works():
                await asyncio.sleep(1)
                async with open_client(relay.url, relay.bob) as worker:
                    claimed = await worker.take(0, uuid4())
                    await asyncio.sleep(2)
                    await worker.call(
                        Finish(message_id=claimed.request.id, status="completed", content="Yes")
                    )

            work = asyncio.create_task(peer_works())
            faults.fail("inspect", times=2)
            loop = asyncio.get_running_loop()
            started = loop.time()
            status = await mcp.call_tool(
                "pigeon_status",
                bound(
                    chat,
                    "pigeon_status",
                    {
                        "conversation_id": str(session),
                        "message_id": follow_up["request"]["id"],
                        "wait": 45,
                    },
                ),
            )
            await work
            assert status.structuredContent["result"]["status"] == "completed", text(status)
            assert status.structuredContent["result"]["response"]["content"] == "Yes"
            assert 2.5 < loop.time() - started < 20
            assert faults.calls["inspect"] >= 3

            # 7. No turn was lost or duplicated.
            rows = relay.store.history("@bob/claude", History(conversation_id=session))["turns"]
            assert [row["status"] for row in rows] == ["completed", "completed"]
            other = relay.store.history("@bob/claude", History(conversation_id=other_session))
            assert other["turns"] == []

            # 10. With the MCP process gone, the CLI finishes a claim for the reported chat.
            third = await ask(peer, inbox, session, "One more question.")
            received = await mcp.call_tool("pigeon_receive", bound(chat, "pigeon_receive"))
            assert received.structuredContent["result"]["request"]["id"] == str(third.id)
            return session, third.id, received.structuredContent["chat"]

    session, request, chat_id = asyncio.run(exchange())
    answered = subprocess.run(
        [
            sys.executable,
            "-m",
            "pigeon.cli",
            "--config",
            str(profile),
            "answer",
            str(request),
            "-",
            "--chat",
            chat_id,
        ],
        input="Answered from the terminal.",
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert answered.returncode == 0, answered.stderr
    assert json.loads(answered.stdout)["status"] == "completed"
