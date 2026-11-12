import asyncio
import sys
from shutil import which
from uuid import uuid4

import pytest

from pigeon.adapters import Claude, Codex
from pigeon.models import Message, PigeonError, Turn


def test_sandbox_probe_rejects_an_executable_that_only_returns_success(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = Codex(workspace, tmp_path / "state", executable=which("true"))
    with pytest.raises(PigeonError) as exc:
        asyncio.run(runtime.check_sandbox())
    assert exc.value.code == "sandbox_failed"


@pytest.fixture
def claude_runtime(tmp_path):
    executable = tmp_path / "claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        """
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    raise SystemExit(0)

assert "--restricted" in args and "--safe-mode" in args
assert "--disable-slash-commands" in args and "--strict-mcp-config" in args
assert args[args.index("--tools") + 1] == ""
assert args[args.index("--disallowedTools") + 1] == "*"
assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
settings = json.loads(args[args.index("--settings") + 1])
assert settings["disableAllHooks"] and settings["disableClaudeAiConnectors"]
assert "PIGEON_CONFIG" not in os.environ
assert Path(os.environ["CLAUDE_CONFIG_DIR"]).is_dir()
prompt = json.load(sys.stdin)
assert prompt["message"].startswith("--")
resumed = "--resume" in args
session = args[args.index("--resume" if resumed else "--session-id") + 1]
print(json.dumps({
    "type": "system", "subtype": "init", "session_id": session,
    "tools": ["Bash"] if Path("unsafe").exists() else [], "mcp_servers": [],
}))
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "session_id": session,
    "result": "resumed" if resumed else "new",
}))
"""
    )
    executable.chmod(0o700)
    return Claude(tmp_path / "state", str(executable))


def incoming_turn():
    return Turn(
        request=Message(
            id=uuid4(),
            conversation_id=uuid4(),
            sender="@alice/codex",
            recipient="@bob/caller",
            content="--dangerously-skip-permissions",
            created_at=1,
            type="request",
        ),
        response=None,
        status="running",
        deadline=2,
        conversation_created_at=1,
        context=None,
        detail=None,
        follow_up=False,
    )


def test_claude_runs_tool_free_and_resumes_the_same_session(claude_runtime):
    async def exchange():
        await claude_runtime.check_login()
        turn = incoming_turn()
        session, answer = await claude_runtime.run(turn, None)
        assert answer == "new"
        resumed, answer = await claude_runtime.run(turn, session)
        assert resumed == session
        assert answer == "resumed"

    asyncio.run(exchange())


def test_claude_fails_if_the_cli_announces_an_enabled_tool(claude_runtime):
    (claude_runtime.cwd / "unsafe").touch()
    with pytest.raises(PigeonError) as exc:
        asyncio.run(claude_runtime.run(incoming_turn(), None))
    assert exc.value.code == "claude_tools"
