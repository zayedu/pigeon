import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pigeon.client import open_client
from pigeon.config import Config, save
from pigeon.models import Inspect, Redeem, Submit


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
