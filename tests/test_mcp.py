import asyncio
import os
import subprocess
import sys
from stat import S_IMODE
from uuid import uuid4

import pytest
from conftest import ALICE, BOB
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pigeon.client import open_client
from pigeon.models import Inspect, PigeonError, Redeem, Submit, Turn


def cli(config, *args):
    return [sys.executable, "-m", "pigeon.cli", "--config", str(config), *args]


def run_cli(config, *args, token=None):
    return subprocess.run(
        cli(config, *args), input=token, text=True, capture_output=True, check=True, timeout=10
    )


def test_cli_daemon_and_stdio_mcp(relay, tmp_path):
    alice, bob = (tmp_path / name for name in ("alice.json", "bob.json"))
    run_cli(alice, "login", relay.url, "--token-stdin", token=relay.alice)
    run_cli(bob, "login", relay.url, "--token-stdin", token=relay.bob)
    assert S_IMODE(bob.stat().st_mode) == 0o600
    run_cli(alice, "expose", "--agent", "echo")
    run_cli(alice, "allow", BOB)
    daemon = subprocess.Popen(cli(alice, "daemon", "run"))

    async def exchange():
        executable, *args = cli(bob, "mcp")
        async with (
            asyncio.timeout(15),
            stdio_client(StdioServerParameters(command=executable, args=args)) as (read, write),
            ClientSession(read, write) as mcp,
        ):
            await mcp.initialize()
            assert {tool.name for tool in (await mcp.list_tools()).tools} == {
                "pigeon_ask",
                "pigeon_reply",
                "pigeon_status",
                "pigeon_cancel",
                "pigeon_connect",
                "pigeon_share",
                "pigeon_receive",
                "pigeon_answer",
            }

            async def call(name, args):
                response = await mcp.call_tool(f"pigeon_{name}", args)
                assert not response.isError, response.content
                return Turn.model_validate(response.structuredContent)

            first = await call(
                "ask",
                {
                    "to": ALICE,
                    "message": "Why cache?",
                    "context": {"type": "github_pr", "repo": "alice/project", "number": 42},
                },
            )
            assert first.response.content == "First turn: Why cache?"
            ids = {
                "conversation_id": str(first.request.conversation_id),
                "message_id": str(first.request.id),
            }
            assert await call("status", ids) == first
            follow_up = await call(
                "reply",
                {
                    "conversation_id": ids["conversation_id"],
                    "message": "For how long?",
                },
            )
            assert follow_up.response.content == "Follow-up: For how long?"
            assert follow_up.context == first.context
            daemon.terminate()
            assert daemon.wait(timeout=5) == 0

            async with open_client(relay.url, relay.bob) as caller:
                queued = await caller.turn(
                    Submit(to=ALICE, message="cancel", conversation_id=uuid4(), message_id=uuid4())
                )
            cancelled = await call(
                "cancel",
                {
                    "conversation_id": str(queued.request.conversation_id),
                    "message_id": str(queued.request.id),
                },
            )
            assert cancelled.status == "cancelled"

    try:
        asyncio.run(exchange())
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait()


def test_invite_and_session_reply(relay, tmp_path):
    receiver = tmp_path / "review.json"
    invite = run_cli(
        receiver, "relay", "invite", "@alice/pr-42", relay.url, "--db", str(relay.store.path)
    ).stdout.strip()
    fake = tmp_path / "claude"
    fake.write_text(
        '#!/bin/sh\n[ "$1" = "--mcp-config" ] && [ "$3" = "--resume" ] && [ "$#" = 3 ]\n'
    )
    fake.chmod(0o700)
    subprocess.run(
        cli(receiver, "join", "claude", "--session", "--allow", BOB),
        env=os.environ | {"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        input=invite + "\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert S_IMODE(receiver.stat().st_mode) == 0o600

    async def exchange():
        executable, *args = cli(receiver, "mcp", "--session")
        async with (
            asyncio.timeout(10),
            stdio_client(StdioServerParameters(command=executable, args=args)) as (read, write),
            ClientSession(read, write) as mcp,
            open_client(relay.url, relay.bob) as caller,
        ):
            await mcp.initialize()
            request = Submit(
                to="@alice/pr-42",
                message="Why did you remove the cache?",
                conversation_id=uuid4(),
                message_id=uuid4(),
            )
            await caller.turn(request)
            received = await mcp.call_tool("pigeon_receive", {})
            assert not received.isError
            turn = Turn.model_validate(received.structuredContent["result"])
            assert turn.request.content == request.message

            unrelated = await mcp.call_tool(
                "pigeon_answer", {"message_id": str(uuid4()), "message": "Wrong request"}
            )
            assert unrelated.isError

            answer = "The cache outlived the session."
            replied = await mcp.call_tool(
                "pigeon_answer", {"message_id": str(request.message_id), "message": answer}
            )
            assert not replied.isError
            completed = await caller.turn(Inspect(conversation_id=request.conversation_id))
            assert completed.status == "completed"
            assert completed.response.content == answer

    asyncio.run(exchange())


def test_session_key_between_two_chats_with_the_same_profile(relay, tmp_path):
    from contextlib import AsyncExitStack

    profile = tmp_path / "shared.json"
    run_cli(profile, "login", relay.url, "--token-stdin", token=relay.alice)
    executable, *args = cli(profile, "mcp")

    async def exchange():
        async with asyncio.timeout(15), AsyncExitStack() as stack:
            chats = []
            for _ in range(2):
                streams = await stack.enter_async_context(
                    stdio_client(StdioServerParameters(command=executable, args=args))
                )
                client = await stack.enter_async_context(ClientSession(*streams))
                await client.initialize()
                chats.append(client)
            owner, reviewer = chats
            shared = await owner.call_tool("pigeon_share", {})
            key = shared.structuredContent["result"]
            connected = await reviewer.call_tool("pigeon_connect", {"key": key})
            destination = connected.structuredContent["result"]
            async with open_client(relay.url, relay.bob) as stranger:
                with pytest.raises(PigeonError, match="in use"):
                    await stranger.call(Redeem(key=key))

            async with asyncio.TaskGroup() as tasks:
                question = tasks.create_task(
                    reviewer.call_tool("pigeon_ask", {"to": destination, "message": "Why cache?"})
                )
                received = await owner.call_tool("pigeon_receive", {})
                turn = Turn.model_validate(received.structuredContent["result"])
                assert turn.request.content == "Why cache?"
                answer = await owner.call_tool(
                    "pigeon_answer",
                    {"message_id": str(turn.request.id), "message": "To save time."},
                )
                assert not answer.isError
            assert Turn.model_validate(question.result().structuredContent).response.content == (
                "To save time."
            )

    asyncio.run(exchange())


def test_setup_creates_named_profile_without_invite(relay, tmp_path):
    from pigeon.config import load

    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\n[ "$2" = "add" ]\n')
    fake.chmod(0o700)
    env = os.environ | {
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
    }
    profile = tmp_path / "william.json"
    args = ("setup", "claude", "--username", "William", "--relay", relay.url)
    subprocess.run(cli(profile, *args), env=env, check=True, capture_output=True, timeout=10)
    config = load(profile)
    assert config.agent == "@william/agent"
    assert S_IMODE(profile.stat().st_mode) == 0o600
    skill = tmp_path / "claude-config" / "skills" / "pigeon" / "SKILL.md"
    assert "pigeon_receive" in skill.read_text()
    collision = subprocess.run(
        cli(tmp_path / "other.json", *args), env=env, capture_output=True, text=True, timeout=10
    )
    assert collision.returncode == 1
    assert "username is taken" in collision.stderr


def test_shared_chat_wakes_without_calling_receive(relay, tmp_path):

    profile = tmp_path / "owner.json"
    run_cli(profile, "login", relay.url, "--token-stdin", token=relay.alice)
    binary = tmp_path / "codex"
    wake_log = tmp_path / "wake.log"
    binary.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{wake_log}"\n')
    binary.chmod(0o700)
    thread = str(uuid4())
    endpoint = f"unix://{tmp_path / 'codex.sock'}"
    executable, *args = cli(
        profile,
        "mcp",
        "--codex-thread",
        thread,
        "--codex-bin",
        str(binary),
        "--codex-endpoint",
        endpoint,
    )

    async def exchange():
        async with (
            asyncio.timeout(10),
            stdio_client(StdioServerParameters(command=executable, args=args)) as streams,
            ClientSession(*streams) as owner,
            open_client(relay.url, relay.bob) as reviewer,
        ):
            await owner.initialize()
            share = await owner.call_tool("pigeon_share", {})
            target = await reviewer.call(Redeem(key=share.structuredContent["result"]))
            question = await reviewer.turn(
                Submit(
                    to=target["address"],
                    conversation_id=uuid4(),
                    message_id=uuid4(),
                    message="Why is this private detail here?",
                )
            )
            while not wake_log.exists() or len(wake_log.read_text().splitlines()) < 7:
                await asyncio.sleep(0.05)
            wake_args = wake_log.read_text().splitlines()
            assert wake_args[:6] == ["queue", "--remote", endpoint, "--thread", thread, "--message"]
            assert question.request.content not in wake_args[6]
            assert (
                await reviewer.turn(Inspect(conversation_id=question.request.conversation_id))
            ).status == "queued"
            received = await owner.call_tool("pigeon_receive", {"wait": 0})
            assert Turn.model_validate(received.structuredContent["result"]).request.id == (
                question.request.id
            )

    asyncio.run(exchange())


def test_cursor_setup_preserves_existing_servers(relay, tmp_path):
    import json

    cursor = tmp_path / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    other = {"url": "https://example.test/mcp"}
    cursor.write_text(json.dumps({"mcpServers": {"other": other}}))
    config_home = tmp_path / "config"
    profile = config_home / "pigeon" / "config.json"
    env = os.environ | {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(config_home)}
    env.pop("PIGEON_CONFIG", None)
    args = ("setup", "cursor", "--username", "reviewer", "--relay", relay.url)
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-m", "pigeon.cli", *args],
            env=env,
            check=True,
            capture_output=True,
            timeout=10,
        )
    skill = tmp_path / ".agents" / "skills" / "pigeon" / "SKILL.md"
    assert "pigeon_connect" in skill.read_text()
    servers = json.loads(cursor.read_text())["mcpServers"]
    assert servers["other"] == other
    assert servers["pigeon-relay"]["args"][-3:] == ["--config", str(profile), "mcp"]


def test_setup_reuses_legacy_hosted_profile(tmp_path):
    import json

    from pigeon.cli import HOSTED_RELAY
    from pigeon.config import Config, save

    config_home = tmp_path / "config"
    profile = config_home / "pigeon" / "hosted-codex.json"
    save(profile, Config(agent="@owner/codex", relay=HOSTED_RELAY, token="a" * 43))
    env = os.environ | {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(config_home)}
    env.pop("PIGEON_CONFIG", None)
    result = subprocess.run(
        [sys.executable, "-m", "pigeon.cli", "setup", "cursor"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "Using your Pigeon account @owner." in result.stdout
    assert not profile.with_name("config.json").exists()
    settings = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert settings["mcpServers"]["pigeon-relay"]["args"][-3:] == ["--config", str(profile), "mcp"]
