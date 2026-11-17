import asyncio
import json
import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from .models import PigeonError
from .wake import WAKE_PROMPT


class ACPClient:
    def __init__(self, process: asyncio.subprocess.Process, name: str):
        self.process = process
        self.name = name
        self.pending = {}
        self.next_id = 0
        self.session_id = None
        self.approval = None

    async def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, method, params, timeout=30):
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                await self.send({"id": request_id, "method": method, "params": params})
                return await future
        finally:
            self.pending.pop(request_id, None)

    async def read(self):
        try:
            await self.read_messages()
        except (OSError, ValueError) as exc:
            raise PigeonError("acp", f"{self.name} sent an invalid ACP response.") from exc
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        PigeonError("acp_closed", f"The {self.name} ACP connection closed.")
                    )

    async def read_messages(self):
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            method = message.get("method")
            if method == "session/update" and "id" not in message:
                params = message["params"]
                update = params["update"]
                if params["sessionId"] == self.session_id and (
                    update.get("sessionUpdate") == "agent_message_chunk"
                    and update["content"]["type"] == "text"
                ):
                    print(update["content"]["text"], end="", flush=True)
            elif method and "id" in message:
                if (
                    method == "session/request_permission"
                    and message["params"]["sessionId"] == self.session_id
                    and self.approval is None
                ):
                    self.approval = message
                    tool = message["params"]["toolCall"].get("title") or "tool call"
                    print(f"\n{self.name} requests permission for {tool}.", flush=True)
                    for i, option in enumerate(message["params"]["options"], 1):
                        print(f"{i}. {option['name']}", flush=True)
                    print("Enter an option number, or press Enter to cancel.", flush=True)
                else:
                    await self.send(
                        {
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Request unavailable"},
                        }
                    )
            elif not method and (future := self.pending.get(message.get("id"))):
                if not future.done():
                    if "error" in message:
                        future.set_exception(
                            PigeonError(
                                "acp",
                                message["error"].get("message", f"{self.name} rejected a request."),
                            )
                        )
                    else:
                        future.set_result(message.get("result"))
        raise PigeonError("acp_closed", f"The {self.name} ACP process exited.")

    async def read_input(self, prompts):
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        try:
            while line := await reader.readline():
                line = line.decode().strip()
                if self.approval is not None:
                    request, self.approval = self.approval, None
                    options = request["params"]["options"]
                    outcome = {"outcome": "cancelled"}
                    if line.isascii() and line.isdecimal() and len(line) <= 3:
                        choice = int(line)
                        if 1 <= choice <= len(options):
                            outcome = {
                                "outcome": "selected",
                                "optionId": options[choice - 1]["optionId"],
                            }
                    await self.send({"id": request["id"], "result": {"outcome": outcome}})
                elif line == "/exit":
                    return
                elif line:
                    await prompts.put(line)
        finally:
            transport.close()

    async def run_prompts(self, prompts):
        while True:
            prompt = await prompts.get()
            try:
                await self.request(
                    "session/prompt",
                    {
                        "sessionId": self.session_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    },
                    timeout=None,
                )
            except PigeonError as exc:
                print(f"\n{self.name}: {exc}", file=sys.stderr)
            print("\n> ", end="", flush=True)


async def run_acp(binary: str, profile: Path, session_id: str | None, *, antigravity: bool = False):
    prompts = asyncio.Queue()
    name = "Antigravity" if antigravity else "Cursor"

    async def enqueue_wake(_reader, writer):
        try:
            prompts.put_nowait(WAKE_PROMPT)
            writer.write(b"1")
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    with tempfile.TemporaryDirectory(prefix="pigeon-", dir="/tmp") as directory:
        endpoint = str(Path(directory) / "wake.sock")
        session_file = Path(endpoint).with_suffix(".session")
        async with await asyncio.start_unix_server(enqueue_wake, path=endpoint):
            proc = await asyncio.create_subprocess_exec(
                binary,
                "acp",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                limit=4 * 1024 * 1024,
            )
            client = ACPClient(proc, name)
            tasks = [asyncio.create_task(client.read())]
            try:
                await client.request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": "pigeon", "version": "0.1.0"},
                    },
                )
                if not antigravity:
                    await client.request("authenticate", {"methodId": "cursor_login"})
                flag = "--antigravity-session-file" if antigravity else "--cursor-endpoint"
                params = {
                    "cwd": os.getcwd(),
                    "mcpServers": [
                        {
                            "name": "pigeon-relay",
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "pigeon.cli",
                                "--config",
                                str(profile),
                                "mcp",
                                flag,
                                str(session_file) if antigravity else endpoint,
                            ],
                            "env": [],
                        }
                    ],
                }
                if session_id is not None:
                    params["sessionId"] = session_id
                resume = "session/resume" if antigravity else "session/load"
                method = resume if session_id else "session/new"
                try:
                    result = await client.request(method, params)
                except PigeonError as exc:
                    if antigravity and str(exc) == "Authentication required":
                        raise PigeonError(
                            "auth_required", "Sign in with agy, then retry."
                        ) from None
                    raise
                returned_id = result.get("sessionId") if isinstance(result, dict) else None
                if session_id and returned_id is not None and returned_id != session_id:
                    raise PigeonError("acp_session", "ACP returned a different session ID.")
                client.session_id = session_id or returned_id
                if not isinstance(client.session_id, str) or not client.session_id.strip():
                    raise PigeonError("acp_session", "ACP did not return a usable session ID.")
                session_file.write_text(client.session_id, encoding="utf-8")
                print(
                    f"{name} session {client.session_id}. Type /exit to leave.\n> ",
                    end="",
                    flush=True,
                )
                tasks.extend(
                    [
                        asyncio.create_task(client.read_input(prompts)),
                        asyncio.create_task(client.run_prompts(prompts)),
                    ]
                )
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    async with asyncio.timeout(5):
                        await proc.wait()
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
