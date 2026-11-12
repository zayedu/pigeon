import asyncio
import subprocess
import sys
from stat import S_IMODE
from uuid import uuid4

from conftest import ALICE, BOB
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pigeon.client import open_client
from pigeon.models import Submit, Turn


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
