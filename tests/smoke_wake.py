"""Opt-in live Codex wake check against an isolated local relay."""

import asyncio
import fcntl
import json
import os
import pty
import shutil
import signal
import struct
import sys
import termios
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from conftest import ALICE, BOB, relay_process
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets.asyncio.client import unix_connect

from pigeon.client import open_client
from pigeon.config import Config, save
from pigeon.db import Store
from pigeon.models import ACTIVE, Inspect, Redeem, Submit


async def app_call(socket, request_id, method, params):
    await socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
    async for data in socket:
        result = json.loads(data)
        if result.get("id") == request_id:
            if "error" in result:
                raise RuntimeError(f"{method}: {result['error']['message']}")
            return result["result"]
    raise RuntimeError("Codex app server closed the connection.")


@asynccontextmanager
async def terminal(codex, endpoint, thread, root):
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(root)
        os.execve(
            codex,
            [
                codex,
                "resume",
                "--remote",
                endpoint,
                "--no-alt-screen",
                "-c",
                "check_for_update_on_startup=false",
                thread,
            ],
            os.environ | {"TERM": "xterm-256color"},
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    tail = b""

    def drain():
        nonlocal tail
        try:
            output = os.read(fd, 4096)
        except OSError:
            output = b""
        if not output:
            loop.remove_reader(fd)
            return
        tail = (tail + output)[-8192:]
        if b"WAKE_SETUP_OK" in tail:
            ready.set()

    loop.add_reader(fd, drain)
    try:
        try:
            await asyncio.wait_for(ready.wait(), 15)
        except TimeoutError:
            raise RuntimeError(
                f"Codex TUI did not resume. Terminal tail: {tail[-1200:]!r}"
            ) from None
        yield
    finally:
        loop.remove_reader(fd)
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        os.close(fd)
        await asyncio.to_thread(os.waitpid, pid, 0)


async def smoke(root, relay, owner_token, peer_token):
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("Install and sign in to Codex before running this check.")
    profile = root / "profile.json"
    save(profile, Config(relay=relay, agent=ALICE, token=owner_token))
    args = ["-m", "pigeon.cli", "--config", str(profile), "mcp"]
    mcp_config = [
        "-c",
        "mcp_servers.pigeon.enabled=false",
        "-c",
        f"mcp_servers.pigeon-relay.command={json.dumps(sys.executable)}",
        "-c",
        f"mcp_servers.pigeon-relay.args={json.dumps(args)}",
        "-c",
        'mcp_servers.pigeon-relay.tools.pigeon_receive.approval_mode="approve"',
        "-c",
        'mcp_servers.pigeon-relay.tools.pigeon_answer.approval_mode="approve"',
    ]
    socket_path = root / "app.sock"
    endpoint = f"unix://{socket_path}"
    proc = await asyncio.create_subprocess_exec(
        codex,
        *mcp_config,
        "app-server",
        "--listen",
        endpoint,
        cwd=root,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(12):
            while not socket_path.is_socket():
                if proc.returncode is not None:
                    raise RuntimeError("Codex app server exited before listening.")
                await asyncio.sleep(0.05)
        async with unix_connect(str(socket_path)) as socket:
            await app_call(
                socket, 1, "initialize", {"clientInfo": {"name": "pigeon-smoke", "version": "0.1"}}
            )
            await socket.send('{"method":"initialized"}')
            started = await app_call(
                socket,
                2,
                "thread/start",
                {
                    "cwd": str(root),
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    "developerInstructions": "Use only the pigeon-relay MCP server for this test.",
                },
            )
            thread = started["thread"]["id"]
            print(f"Loaded disposable Codex thread {thread}.", flush=True)
            await app_call(
                socket,
                3,
                "turn/start",
                {
                    "threadId": thread,
                    "input": [{"type": "text", "text": "Reply WAKE_SETUP_OK. Use no tools."}],
                },
            )
            async with asyncio.timeout(30):
                async for data in socket:
                    event = json.loads(data)
                    if event.get("method") == "turn/completed":
                        if event["params"]["turn"]["status"] != "completed":
                            raise RuntimeError("Codex warm-up turn failed.")
                        break
            wake_args = [
                "--codex-thread",
                thread,
                "--codex-bin",
                codex,
                "--codex-endpoint",
                endpoint,
            ]
            async with (
                terminal(codex, endpoint, thread, root),
                stdio_client(
                    StdioServerParameters(command=sys.executable, args=args + wake_args)
                ) as streams,
                ClientSession(*streams) as mcp,
                open_client(relay, peer_token) as peer,
            ):
                await mcp.initialize()
                shared = await mcp.call_tool("pigeon_share")
                assert not shared.isError
                joined = await peer.call(
                    Redeem(key=shared.structuredContent["result"], peer_session=True)
                )
                sent = await peer.turn(
                    Submit(
                        to=joined["address"],
                        conversation_id=joined["conversation_id"],
                        message_id=uuid4(),
                        message="Answer this smoke test with PIGEON_WAKE_OK.",
                    )
                )
                query = Inspect(
                    conversation_id=sent.request.conversation_id, message_id=sent.request.id
                )
                for _ in range(6):
                    if sent.status not in ACTIVE:
                        break
                    sent = await peer.turn(
                        query.model_copy(update={"after": sent.status, "wait": 5})
                    )
                    print(f"Request status: {sent.status}", flush=True)
                if sent.status != "completed" or "PIGEON_WAKE_OK" not in sent.response.content:
                    status = await mcp.call_tool("pigeon_status")
                    wake = status.structuredContent["result"]["wake"]
                    raise RuntimeError(
                        f"Idle wake smoke ended at {sent.status}. Wake state: {wake}"
                    )
                print("Codex woke, claimed the request, and returned the expected response.")
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="pigeon-smoke-", dir="/tmp") as tmp:
        root = Path(tmp)
        store = Store(root / "relay.sqlite3")
        owner, peer = store.enroll(ALICE), store.enroll(BOB)
        with relay_process(store) as relay:
            asyncio.run(smoke(root, relay, owner, peer))
