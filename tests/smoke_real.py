"""Opt-in Claude Code → Pigeon → Codex smoke test. Requires two provider logins."""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from pigeon.client import open_client
from pigeon.config import load
from pigeon.models import ACTIVE, Inspect, ListCalls

TOOLS = ("mcp__pigeon__pigeon_ask", "mcp__pigeon__pigeon_reply", "mcp__pigeon__pigeon_status")


def run_claude(executable, config, prompt, expected_tool, cwd):
    completed = subprocess.run(
        [
            executable,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--strict-mcp-config",
            "--mcp-config",
            str(config),
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--allowedTools",
            ",".join(TOOLS),
            "--",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=160,
        cwd=cwd,
        check=False,
    )
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    if completed.returncode:
        result = next(
            (event.get("result") for event in reversed(events) if event.get("type") == "result"),
            None,
        )
        raise SystemExit(f"Claude Code failed: {result or completed.stderr.strip() or 'no output'}")
    if not any(
        item.get("type") == "tool_use" and item.get("name") == expected_tool
        for event in events
        if event.get("type") == "assistant"
        for item in event.get("message", {}).get("content", [])
    ):
        raise SystemExit(f"Claude Code did not call {expected_tool}.")


async def recent(config):
    async with open_client(config.relay, config.token) as client:
        return await client.call(ListCalls())


async def verify(config, row, expected):
    async with asyncio.timeout(125), open_client(config.relay, config.token) as client:
        query = Inspect(
            conversation_id=UUID(row["conversation_id"]), message_id=UUID(row["message_id"])
        )
        turn = await client.turn(query)
        while turn.status in ACTIVE:
            query = query.model_copy(update={"after": turn.status, "wait": 20})
            turn = await client.turn(query)
    if turn.status != "completed" or expected not in turn.response.content:
        detail = turn.response.content if turn.response else turn.detail
        raise SystemExit(f"Unexpected Codex outcome ({turn.status}): {detail}")
    print(f"Verified {turn.request.id} returned {expected!r}.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Caller profile")
    parser.add_argument("--to", required=True, help="Codex daemon's address")
    parser.add_argument("--expect", required=True, help="Value in receiver's PIGEON_SMOKE.txt")
    parser.add_argument("--claude", default="claude", help="Claude Code executable")
    args = parser.parse_args()
    executable = shutil.which(args.claude)
    if not executable:
        raise SystemExit("Claude Code is not installed.")
    profile = args.config.expanduser().resolve()
    config = load(profile)
    nonce = f"pigeon-{uuid4().hex[:12]}"

    with tempfile.TemporaryDirectory(prefix="pigeon-smoke-") as directory:
        cwd = Path(directory)
        mcp_config = cwd / "mcp.json"
        mcp_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "pigeon": {
                            "command": sys.executable,
                            "args": ["-m", "pigeon.cli", "--config", str(profile), "mcp"],
                        }
                    }
                }
            )
        )
        before = {row["conversation_id"] for row in asyncio.run(recent(config))}
        run_claude(
            executable,
            mcp_config,
            f"Use pigeon_ask to ask {args.to} exactly this question once: "
            f"What value is in PIGEON_SMOKE.txt? Read it and give the exact value. "
            f"Also remember the word {nonce} for a follow-up. "
            "You can use pigeon_status to check progress.",
            TOOLS[0],
            cwd,
        )
        opened = [
            row
            for row in asyncio.run(recent(config))
            if row["conversation_id"] not in before and row["recipient"] == args.to
        ]
        if len(opened) != 1:
            raise SystemExit("Expected one new Pigeon conversation.")
        first = opened[0]
        asyncio.run(verify(config, first, args.expect))

        run_claude(
            executable,
            mcp_config,
            f"Use pigeon_reply in conversation {first['conversation_id']} exactly once. "
            "Ask which word the recipient was told to remember in the previous question. "
            "You can use pigeon_status to check progress.",
            TOOLS[1],
            cwd,
        )
        follow_up = next(
            row
            for row in asyncio.run(recent(config))
            if row["conversation_id"] == first["conversation_id"]
        )
        if follow_up["message_id"] == first["message_id"]:
            raise SystemExit("The follow-up did not reach Pigeon.")
        asyncio.run(verify(config, follow_up, nonce))
        print(f"Claude Code and Codex completed conversation {first['conversation_id']}.")


if __name__ == "__main__":
    main()
