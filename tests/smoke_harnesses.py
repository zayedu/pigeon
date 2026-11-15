"""Run live Pigeon checks with installed coding agents and a disposable relay."""

import argparse
import asyncio
import fcntl
import json
import os
import pty
import re
import shutil
import signal
import struct
import sys
import termios
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from conftest import ALICE, BOB, relay_process
from smoke_wake import attachment_round_trip, exchange
from smoke_wake import smoke as codex_wake

from pigeon.client import open_client
from pigeon.config import Config, save
from pigeon.db import Store
from pigeon.models import Capabilities, Redeem

HARNESSES = ("codex", "claude", "cursor", "antigravity")
SERVER = "pigeon-relay"
KEY = rb"PIGEON_READY[ `*]*(pg_[A-Za-z0-9_-]{22})"
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][A-Za-z])")
ACP_APPROVAL = re.compile(
    rb"requests permission for (?:Run )?(?:mcp_+pigeon-relay_+)?"
    rb"pigeon_(?:share|receive|answer)\.\n(\d+)\. Allow(?: Once)?\n"
    rb"[^\r]*?Enter an option number, or press Enter to cancel\.",
    re.IGNORECASE | re.DOTALL,
)
SHARE_PROMPT = (
    "Call the pigeon_share MCP tool exactly once. Print PIGEON_READY followed by the "
    "returned key, on one line. Then stop. Do not poll. You are authorized to answer "
    "the synthetic Pigeon test requests with the requested token using pigeon_answer."
)
INBOX_PROMPT = (
    "Call pigeon_receive once with wait=0. Answer the pending synthetic message with "
    "pigeon_answer and the requested token."
)


@asynccontextmanager
async def terminal(command, cwd, env, *, acp):
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
            os.execvpe(command[0], command, env)
        except OSError:
            os._exit(127)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 220, 0, 0))
    loop = asyncio.get_running_loop()
    changed = asyncio.Event()
    output = bytearray()
    closed = False
    enter = None
    approved_through = 0

    def drain():
        nonlocal closed, approved_through
        try:
            data = os.read(fd, 8192)
        except OSError:
            data = b""
        if data:
            output.extend(data)
            if acp and (approval := ACP_APPROVAL.search(screen(), approved_through)):
                approved_through = approval.end()
                send(approval[1].decode())
        else:
            closed = True
            loop.remove_reader(fd)
        changed.set()

    def send(line):
        nonlocal enter
        if line:
            os.write(fd, line.encode())
        enter = loop.call_later(0.15, os.write, fd, b"\r")

    def screen():
        return ANSI.sub(b"", bytes(output)).replace(b"\r", b"")

    async def expect(pattern, *, after=0):
        while True:
            changed.clear()
            cleaned = screen()
            if match := pattern.search(cleaned, after):
                return match
            if re.search(
                rb"(?i)usage limit|out of usage|quota exceeded|rate limit|usage_limit|"
                rb"add a payment method to continue",
                cleaned,
            ):
                raise RuntimeError("The harness reported a usage or billing limit.")
            if re.search(rb"(?i)authentication required|sign in with agy|not logged in", cleaned):
                raise RuntimeError("The harness requires authentication.")
            if closed:
                raise RuntimeError("The harness exited before the expected output.")
            await changed.wait()

    loop.add_reader(fd, drain)
    try:
        yield send, expect
    finally:
        if enter:
            enter.cancel()
        loop.remove_reader(fd)
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        os.close(fd)
        try:
            async with asyncio.timeout(3):
                while not os.waitpid(pid, os.WNOHANG)[0]:
                    await asyncio.sleep(0.05)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            await asyncio.to_thread(os.waitpid, pid, 0)


def claude_command(root, profile, model):
    binary = shutil.which("claude")
    if binary is None:
        raise RuntimeError("Install Claude Code first.")
    config = root / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    SERVER: {
                        "command": sys.executable,
                        "args": ["-m", "pigeon.cli", "--config", str(profile), "mcp"],
                    },
                },
            }
        )
    )
    tool_names = ("share", "receive", "answer")
    return [
        binary,
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--debug-file",
        str(root / "claude.log"),
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ",".join(f"mcp__{SERVER}__pigeon_{name}" for name in tool_names),
        *(["--model", model] if model else []),
    ]


async def wait_for_log(path, text):
    while not path.is_file() or text not in path.read_text():
        await asyncio.sleep(0.1)


def claude_share_key(home, root):
    project = str(root.resolve()).replace("/", "-")
    for transcript in (home / "projects" / project).glob("*.jsonl"):
        share_calls = set()
        for line in transcript.read_bytes().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            content = event.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("name") == f"mcp__{SERVER}__pigeon_share":
                    share_calls.add(block["id"])
                if block.get("tool_use_id") in share_calls and not block.get("is_error"):
                    if key := re.search(r"pg_[A-Za-z0-9_-]{22}", json.dumps(block["content"])):
                        return key[0]
    raise RuntimeError("Claude Code's first turn ended without a successful Pigeon share.")


async def check_chat(harness, root, relay, owner, reviewer, args):
    profile = root / "profile.json"
    save(profile, Config(relay=relay, agent=ALICE, token=owner))
    env = os.environ | {"TERM": "xterm-256color"}
    if harness == "claude":
        claude_home = Path(env.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
        command = claude_command(root, profile, args.claude_model)
        env |= {"ENABLE_TOOL_SEARCH": "false"}
        if args.ollama:
            claude_home = root / "claude"
            claude_home.mkdir()
            (claude_home / ".claude.json").write_text(
                json.dumps(
                    {
                        "hasCompletedOnboarding": True,
                        "theme": "dark",
                        "projects": {str(root.resolve()): {"hasTrustDialogAccepted": True}},
                    }
                )
            )
            env |= {
                "CLAUDE_CONFIG_DIR": str(claude_home),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:11434",
                "ANTHROPIC_AUTH_TOKEN": "ollama",
                "ANTHROPIC_API_KEY": "",
            }
    else:
        command = [sys.executable, "-m", "pigeon.cli", "--config", str(profile)]
        command += ["wake", "cursor"] if harness == "cursor" else ["antigravity"]

    async with terminal(command, root, env, acp=harness != "claude") as (send, expect):
        async with asyncio.timeout(args.timeout):
            if harness == "claude":
                ready = await expect(re.compile(rb"Accessing[ ]*workspace|Claude[ ]*Codev"))
                if ready.group().startswith(b"Accessing"):
                    send("\x1b[B")
                    await expect(re.compile(b"ClaudeCodev|Claude Code"))
                await wait_for_log(root / "claude.log", "Successfully connected (transport:")
            else:
                await expect(re.compile(rb"Type /exit to leave\."))
            print(f"{harness}: started chat; requesting MCP share.", flush=True)
            send(SHARE_PROMPT)
            if harness == "claude":
                await wait_for_log(root / "claude.log", "[engine] turn 1 end")
                key = claude_share_key(claude_home, root)
            else:
                shared = await expect(re.compile(KEY + rb".*?\n> ", re.DOTALL))
                key = shared.group(1).decode()
                idle_after = shared.end()
        print(f"{harness}: MCP share passed.", flush=True)

        async with open_client(relay, reviewer) as peer:
            joined = await peer.call(Redeem(key=key, peer_session=True))
            capabilities = await peer.call(Capabilities(conversation_id=joined["conversation_id"]))
            wake = "wake" in (capabilities["features"] or [])
            if harness != "antigravity" and not wake:
                raise RuntimeError("The shared chat did not attach its wake listener.")
            for follow_up in (False, True):
                stage = "follow-up" if follow_up else "first message"
                delivery = "wake" if wake else "inbox check"
                print(f"{harness}: checking {stage}.", flush=True)
                async with asyncio.timeout(args.timeout + 5):
                    await exchange(
                        peer,
                        joined,
                        args.timeout,
                        follow_up=follow_up,
                        check_inbox=None if wake else lambda: send(INBOX_PROMPT),
                    )
                    if not follow_up:
                        if harness == "claude":
                            await wait_for_log(root / "claude.log", "[engine] turn 2 end")
                        else:
                            idle_after = (
                                await expect(re.compile(rb"\n> "), after=idle_after)
                            ).end()
                print(f"{harness}: {stage} via {delivery} passed.", flush=True)
            await attachment_round_trip(profile, peer, reviewer, joined)
            print(f"{harness}: binary attachment round trip passed.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=HARNESSES, nargs="*")
    parser.add_argument("--timeout", type=int, metavar="SECONDS", default=180)
    parser.add_argument("--ollama", action="store_true", help="Use local Ollama for Claude Code.")
    parser.add_argument("--claude-model", help="Override the Claude Code model.")
    args = parser.parse_args()
    if not 10 <= args.timeout <= 900:
        parser.error("--timeout must be between 10 and 900 seconds")
    if args.ollama and not args.claude_model:
        args.claude_model = "qwen3.5:4b"
    failures = []
    for harness in args.harness or HARNESSES:
        try:
            with TemporaryDirectory(prefix="pigeon-harness-", dir="/tmp") as directory:
                root = Path(directory)
                store = Store(root / "relay.sqlite3")
                owner, reviewer = store.enroll(ALICE), store.enroll(BOB)
                with relay_process(store) as relay:
                    if harness == "codex":
                        asyncio.run(codex_wake(root, relay, owner, reviewer, timeout=args.timeout))
                    else:
                        asyncio.run(check_chat(harness, root, relay, owner, reviewer, args))
        except (Exception, SystemExit) as exc:
            detail = str(exc) if isinstance(exc, (RuntimeError, SystemExit)) else type(exc).__name__
            failures.append(harness)
            print(f"{harness}: FAILED ({detail or type(exc).__name__}).", file=sys.stderr)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
