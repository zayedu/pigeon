from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .client import ask_agent, open_client
from .config import Config
from .models import Address, Cancel, Content, Context, Inspect, PigeonError, Turn


def create_mcp(config: Config) -> FastMCP:
    server = FastMCP("Pigeon")

    @server.tool()
    async def pigeon_ask(to: Address, message: Content, context: Context | None = None) -> Turn:
        """Ask a trusted agent a question. Use pigeon_status if the turn is still active."""
        try:
            return await ask_agent(config.relay, config.token, message, to=to, context=context)
        except PigeonError as exc:
            raise ToolError(str(exc)) from None

    @server.tool()
    async def pigeon_reply(conversation_id: UUID, message: Content) -> Turn:
        """Ask a follow-up in a conversation whose previous turn completed."""
        try:
            return await ask_agent(
                config.relay, config.token, message, conversation_id=conversation_id
            )
        except PigeonError as exc:
            raise ToolError(str(exc)) from None

    @server.tool()
    async def pigeon_status(conversation_id: UUID, message_id: UUID | None = None) -> Turn:
        """Read the latest turn or look up a specific message in a conversation."""
        try:
            async with open_client(config.relay, config.token) as client:
                return await client.turn(
                    Inspect(conversation_id=conversation_id, message_id=message_id)
                )
        except PigeonError as exc:
            raise ToolError(str(exc)) from None

    @server.tool()
    async def pigeon_cancel(conversation_id: UUID, message_id: UUID) -> Turn:
        """Cancel an active request you sent. Use the request ID as message_id."""
        try:
            async with open_client(config.relay, config.token) as client:
                return Turn.model_validate(
                    await client.call(
                        Cancel(conversation_id=conversation_id, message_id=message_id)
                    )
                )
        except PigeonError as exc:
            raise ToolError(str(exc)) from None

    return server
