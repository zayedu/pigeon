import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pigeon.client import open_client
from pigeon.config import Config, save
from pigeon.cursor import hook_installed
from pigeon.models import Capabilities, Finish, Inspect, PigeonError, Redeem, Submit, Take


def test_cursor_wake_bridges_to_the_right_inbox(relay, tmp_path):
    profile = tmp_path / "cursor.json"
    save(profile, Config(relay=relay.url, agent="@alice/codex", token=relay.alice))

    async def exchange(endpoint):
        notified = asyncio.Event()
        endpoint.with_suffix(".session").write_text(str(uuid4()))

        async def acknowledge(_reader, writer):
            writer.write(b"1")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            notified.set()

        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "pigeon.cli",
                "--config",
                str(profile),
                "mcp",
                "--cursor-endpoint",
                str(endpoint),
            ],
        )
        async with (
            asyncio.timeout(10),
            await asyncio.start_unix_server(acknowledge, path=endpoint),
            stdio_client(params) as streams,
            ClientSession(*streams) as cursor,
            open_client(relay.url, relay.bob) as peer,
        ):
            await cursor.initialize()
            shared = await cursor.call_tool("pigeon_share")
            assert not shared.isError
            assert shared.structuredContent["wake"]["attached"]
            joined = await peer.call(
                Redeem(key=shared.structuredContent["result"], peer_session=True)
            )
            request = Submit(
                to=joined["address"],
                conversation_id=joined["conversation_id"],
                message_id=uuid4(),
                message="Synthetic Cursor question.",
            )
            assert (await peer.turn(request)).status == "queued"
            await notified.wait()
            received = await cursor.call_tool("pigeon_receive", {"wait": 0})
            assert str(request.message_id) in received.model_dump_json()
            answer = await cursor.call_tool(
                "pigeon_answer", {"message_id": str(request.message_id), "message": "Cursor reply."}
            )
            assert not answer.isError
            result = await peer.turn(
                Inspect(conversation_id=request.conversation_id, message_id=request.message_id)
            )
            assert result.status == "completed"
            assert result.response.content == "Cursor reply."

    with TemporaryDirectory(prefix="pigeon-cursor-test-", dir="/tmp") as directory:
        asyncio.run(exchange(Path(directory) / "wake.sock"))


def bind(conversation_id, tool="pigeon_share", arguments=None):
    from pigeon import cursor

    event = {
        "hook_event_name": "preToolUse",
        "tool_name": f"MCP:{tool}",
        "conversation_id": conversation_id,
        "tool_input": dict(arguments or {}, chat_binding="forged"),
    }
    result = cursor.hook(event)
    assert result["permission"] == "allow"
    return result["updated_input"]


def test_cursor_hook_signs_only_pigeon_tools(tmp_path, monkeypatch):
    from pigeon import cursor

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cursor.hook({"hook_event_name": "preToolUse", "tool_name": "Shell"}) == {
        "permission": "allow"
    }
    denied = cursor.hook({"hook_event_name": "preToolUse", "tool_name": "MCP:pigeon_share"})
    assert denied["permission"] == "deny"
    arguments = bind("chat-a", "pigeon_ask", {"message": "hi"})
    assert arguments["message"] == "hi"
    assert cursor.verify(arguments["chat_binding"]) == "chat-a"
    conversation, _, signature = arguments["chat_binding"].rpartition(".")
    for forged in ("chat-a", f"chat-b.{signature}", "chat-a." + "0" * 64, ".".join(["x"] * 3)):
        with pytest.raises(PigeonError):
            cursor.verify(forged)
    # A string tool input, as beforeMCPExecution sends, is accepted too.
    event = {
        "hook_event_name": "preToolUse",
        "tool_name": "MCP:pigeon_status",
        "conversation_id": "chat-a",
        "tool_input": json.dumps({"wait": 5}),
    }
    assert cursor.hook(event)["updated_input"]["wait"] == 5


def test_cursor_desktop_chats_have_isolated_resumable_inboxes(relay, tmp_path):
    profile = tmp_path / "config" / "pigeon" / "config.json"
    save(profile, Config(relay=relay.url, agent="@alice/codex", token=relay.alice))
    environment = os.environ | {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        # Inherited from a terminal. Cursor mode must not treat it as a chat identity.
        "CLAUDE_CODE_SESSION_ID": str(uuid4()),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pigeon.cli", "--config", str(profile), "mcp", "--cursor"],
        env=environment,
    )
    first_chat, second_chat = str(uuid4()), str(uuid4())

    async def call(cursor, chat, tool, arguments=None):
        previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = environment["XDG_CONFIG_HOME"]
        try:
            bound = bind(chat, tool, arguments)
        finally:
            if previous is None:
                os.environ.pop("XDG_CONFIG_HOME")
            else:
                os.environ["XDG_CONFIG_HOME"] = previous
        return await cursor.call_tool(tool, bound)

    async def first_window():
        async with (
            asyncio.timeout(20),
            stdio_client(params) as streams,
            ClientSession(*streams) as cursor,
            open_client(relay.url, relay.bob) as peer,
        ):
            await cursor.initialize()
            unbound = await cursor.call_tool("pigeon_share", {})
            assert unbound.isError and "Pigeon restored it" in unbound.content[0].text
            assert hook_installed(tmp_path / ".cursor" / "hooks.json")
            unbound = await cursor.call_tool("pigeon_share", {})
            assert unbound.isError and "did not run for this call" in unbound.content[0].text
            forged = await cursor.call_tool("pigeon_share", {"chat_binding": first_chat})
            assert forged.isError and "not signed" in forged.content[0].text

            sessions = {}
            for chat in (first_chat, second_chat):
                shared = await call(cursor, chat, "pigeon_share")
                assert not shared.isError
                assert not shared.structuredContent["wake"]["attached"]
                sessions[chat] = await peer.call(
                    Redeem(key=shared.structuredContent["result"], peer_session=True)
                )
            assert sessions[first_chat]["address"] != sessions[second_chat]["address"]

            long_question = "Explain the gradient noise scale. " * 400
            request = Submit(
                to=sessions[first_chat]["address"],
                conversation_id=sessions[first_chat]["conversation_id"],
                message_id=uuid4(),
                message=long_question,
            )
            assert (await peer.turn(request)).status == "queued"

            other = await call(cursor, second_chat, "pigeon_receive", {"wait": 0})
            assert other.structuredContent == {"result": None}
            denied = await call(
                cursor,
                second_chat,
                "pigeon_history",
                {"conversation_id": sessions[first_chat]["conversation_id"]},
            )
            assert denied.isError

            collided = await call(cursor, first_chat, "pigeon_reply", {"message": "Me first"})
            assert collided.isError
            assert "pigeon_receive" in collided.content[0].text
            assert str(request.message_id) in collided.content[0].text

            received = await call(cursor, first_chat, "pigeon_receive", {"wait": 0})
            assert received.structuredContent["result"]["request"]["content"] == long_question
            return sessions, request

    async def after_restart(sessions, request):
        async with (
            asyncio.timeout(20),
            stdio_client(params) as streams,
            ClientSession(*streams) as cursor,
            open_client(relay.url, relay.bob) as peer,
        ):
            await cursor.initialize()
            listed = await call(cursor, first_chat, "pigeon_status")
            assert [
                row["conversation_id"] for row in listed.structuredContent["result"]["sessions"]
            ] == [sessions[first_chat]["conversation_id"]]
            assert listed.structuredContent["result"]["chat"] == f"cursor:{first_chat}"
            extended = await call(
                cursor, first_chat, "pigeon_extend", {"message_id": str(request.message_id)}
            )
            assert extended.structuredContent["status"] == "running"
            answer = "A long answer. " * 700
            answered = await call(
                cursor,
                first_chat,
                "pigeon_answer",
                {"message_id": str(request.message_id), "message": answer},
            )
            assert answered.isError and "at most 4,000 characters" in answered.content[0].text
            await peer.call(Capabilities(features=["long_messages"]))
            answered = await call(
                cursor,
                first_chat,
                "pigeon_answer",
                {"message_id": str(request.message_id), "message": answer},
            )
            assert not answered.isError, answered.content[0].text
            result = await peer.turn(
                Inspect(conversation_id=request.conversation_id, message_id=request.message_id)
            )
            assert result.status == "completed" and result.response.content == answer
            history = await call(
                cursor,
                first_chat,
                "pigeon_history",
                {"conversation_id": sessions[first_chat]["conversation_id"]},
            )
            assert [
                turn["request"]["id"] for turn in json.loads(history.content[0].text)["turns"]
            ] == [str(request.message_id)]
            listed = await call(cursor, second_chat, "pigeon_status")
            assert [
                row["conversation_id"] for row in listed.structuredContent["result"]["sessions"]
            ] == [sessions[second_chat]["conversation_id"]]

    sessions, request = asyncio.run(first_window())
    asyncio.run(after_restart(sessions, request))


def test_sender_follows_a_slow_peer_with_long_polling(relay, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    profile = tmp_path / "config" / "pigeon" / "config.json"
    save(profile, Config(relay=relay.url, agent="@alice/codex", token=relay.alice))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pigeon.cli", "--config", str(profile), "mcp", "--cursor"],
        env=dict(os.environ),
    )
    chat = str(uuid4())

    async def exchange():
        async with (
            asyncio.timeout(40),
            stdio_client(params) as streams,
            ClientSession(*streams) as cursor,
            open_client(relay.url, relay.bob) as peer,
        ):
            await cursor.initialize()
            shared = await cursor.call_tool("pigeon_share", bind(chat))
            await peer.call(Redeem(key=shared.structuredContent["result"], peer_session=True))
            asked = await cursor.call_tool(
                "pigeon_ask", bind(chat, "pigeon_ask", {"message": "Run the sweep.", "wait": 0})
            )
            sent = asked.structuredContent
            assert sent["status"] == "queued"
            follow = {
                "conversation_id": sent["request"]["conversation_id"],
                "message_id": sent["request"]["id"],
                "wait": 30,
            }

            async def peer_works():
                await asyncio.sleep(1)
                claimed = await peer.call(Take(wait=0))
                await asyncio.sleep(2)
                await peer.call(
                    Finish(message_id=claimed["request"]["id"], status="completed", content="Done")
                )

            work = asyncio.create_task(peer_works())
            loop = asyncio.get_running_loop()
            started = loop.time()
            # An explicit after returns on the transition.
            running = await cursor.call_tool(
                "pigeon_status", bind(chat, "pigeon_status", follow | {"after": "queued"})
            )
            assert running.structuredContent["result"]["status"] == "running"
            assert 0.5 < loop.time() - started < 10
            # Without after, one call waits through the claim to the answer.
            done = await cursor.call_tool("pigeon_status", bind(chat, "pigeon_status", follow))
            assert done.structuredContent["result"]["status"] == "completed"
            assert done.structuredContent["result"]["response"]["content"] == "Done"
            assert loop.time() - started < 15
            await work

    asyncio.run(exchange())


def test_concurrent_first_hooks_share_one_complete_key(tmp_path):
    event = json.dumps(
        {
            "hook_event_name": "preToolUse",
            "tool_name": "MCP:pigeon_share",
            "conversation_id": "first-use",
            "tool_input": {},
        }
    )
    environment = os.environ | {"XDG_CONFIG_HOME": str(tmp_path)}
    hooks = [
        subprocess.Popen(
            [sys.executable, "-m", "pigeon.cli", "cursor-hook"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=environment,
        )
        for _ in range(16)
    ]
    # Release every hook at once instead of one at a time.
    for hook in hooks:
        hook.stdin.write(event)
    for hook in hooks:
        hook.stdin.close()
    results = [json.loads(hook.stdout.read()) for hook in hooks]
    assert [hook.wait(timeout=30) for hook in hooks] == [0] * len(hooks)
    assert {result["permission"] for result in results} == {"allow"}
    assert len({result["updated_input"]["chat_binding"] for result in results}) == 1
    assert len((tmp_path / "pigeon" / "cursor-hook.key").read_bytes()) == 32
    assert not list((tmp_path / "pigeon").glob(".cursor-hook-*"))
