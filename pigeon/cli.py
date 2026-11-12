import asyncio
import hashlib
import json
import logging
import shlex
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import typer
from pydantic import ValidationError

from .adapters import Codex
from .client import ask_agent, open_client, relay_endpoint
from .config import (
    Config,
    Exposure,
    config_lock,
    default_config,
    load,
    local_allowlist,
    local_lock,
    save,
    state_path,
    validate_token,
)
from .daemon import Daemon, Echo, Sessions
from .db import Store
from .models import Cancel, Command, Context, Grant, Inspect, ListCalls, PigeonError, Ping, address
from .server import serve

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
relay_app = typer.Typer(no_args_is_help=True, help="Run a relay and manage its agent credentials.")
app.add_typer(relay_app, name="relay")

Database = Annotated[Path, typer.Option("--db", help="Relay SQLite database.")]
Wait = Annotated[int, typer.Option(min=0, max=60, help="Seconds to wait for a response.")]


@app.callback()
def root(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option(envvar="PIGEON_CONFIG")] = None,
):
    """Let your agent talk to mine."""
    ctx.obj = (config or default_config()).expanduser().absolute()


@relay_app.command("serve")
def start_relay(
    db: Database,
    host: str = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    cert: Path | None = None,
    key: Path | None = None,
):
    """Start the relay in the foreground."""
    serve(db, host, port, cert, key)


@relay_app.command()
def enroll(agent: str, db: Database, rotate: bool = False):
    """Issue a token for an agent. Share it privately."""
    typer.echo(Store(db).enroll(agent, rotate=rotate))


@relay_app.command("revoke")
def revoke_token(agent: str, db: Database):
    """Disable an agent's token and cancel its active turns."""
    Store(db).revoke(address.validate_python(agent))
    typer.echo(f"Revoked {agent}.")


async def rpc(config: Config, command: Command):
    async with open_client(config.relay, config.token) as client:
        return await client.call(command)


def show(value):
    if hasattr(value, "model_dump_json"):
        typer.echo(value.model_dump_json(indent=2))
    else:
        typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


@app.command()
def login(
    ctx: typer.Context,
    relay: str,
    token_stdin: Annotated[bool, typer.Option("--token-stdin")] = False,
):
    """Save an enrollment token and verify its identity."""
    url = relay_endpoint(relay).removesuffix("/v1/ws")
    token = (
        sys.stdin.readline().strip()
        if token_stdin
        else typer.prompt("Agent token", hide_input=True)
    )
    validate_token(token)
    config = Config.model_construct(relay=url, token=token)
    agent = asyncio.run(rpc(config, Ping()))["agent"]
    with config_lock(ctx.obj):
        if ctx.obj.exists():
            config = load(ctx.obj)
            if (config.relay, config.agent) != (url, agent):
                raise PigeonError(
                    "different_identity", "Use a different --config for a new relay or agent."
                )
            config = config.model_copy(update={"token": token})
        else:
            config = Config(relay=url, agent=agent, token=token)
        save(ctx.obj, config)
    typer.echo(f"Connected as {agent}.")


@app.command()
def expose(
    ctx: typer.Context,
    agent: Annotated[Literal["codex", "echo"], typer.Option("--agent")],
    repo: Path | None = None,
    timeout: Annotated[int, typer.Option(min=1, max=900)] = 120,
    concurrency: Annotated[int, typer.Option(min=1, max=4)] = 1,
    codex: str = "codex",
):
    """Configure the runtime for this identity."""
    if (agent == "codex") != (repo is not None):
        raise PigeonError("invalid_workspace", "Supply --repo for Codex. Echo uses no workspace.")
    workspace = repo.resolve() if repo else None
    with config_lock(ctx.obj):
        config = load(ctx.obj)
        if workspace:
            runtime = Codex(workspace, state_path(ctx.obj) / "codex", codex)
            codex = runtime.binary
        config.exposure = Exposure(
            runtime=agent,
            workspace=workspace,
            executable=codex,
            timeout=timeout,
            concurrency=concurrency,
        )
        save(ctx.obj, config)
    typer.echo(f"Configured {config.agent} to use {agent}.")
    if workspace:
        typer.echo("Sign in to the dedicated Codex home if needed.")
        typer.echo(
            f"CODEX_HOME={shlex.quote(str(runtime.home))} "
            + shlex.join([runtime.binary, "-c", 'cli_auth_credentials_store="file"', "login"])
        )
    typer.echo("Run pigeon allow for each peer, then pigeon daemon run.")


def set_permission(path: Path, sender: str, allow: bool):
    operation = Grant(op="allow" if allow else "deny", sender=sender)
    with config_lock(path):
        config = load(path)
        if sender == config.agent:
            raise PigeonError("invalid_peer", "Choose another registered agent.")
        # Local denial takes effect even if the relay is temporarily unavailable.
        if not allow:
            config.allowed = config.allowed - {sender}
            save(path, config)
        asyncio.run(rpc(config, operation))
        if allow:
            config.allowed = config.allowed | {sender}
            save(path, config)


@app.command()
def allow(ctx: typer.Context, sender: str):
    """Allow a peer on the relay and in the local daemon."""
    set_permission(ctx.obj, sender, True)
    typer.echo(f"Allowed {sender}.")


@app.command()
def deny(ctx: typer.Context, sender: str):
    """Revoke a peer's access and cancel their active turns."""
    set_permission(ctx.obj, sender, False)
    typer.echo(f"Revoked access for {sender}.")


daemon_app = typer.Typer(no_args_is_help=True, help="Run or inspect the local receiver.")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("run")
def run_daemon(ctx: typer.Context):
    """Receive requests and run the configured local runtime."""
    config = load(ctx.obj)
    exposure = config.exposure
    if exposure is None:
        raise PigeonError("not_exposed", "Run pigeon expose to configure a runtime.")
    state = state_path(ctx.obj)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime = (
        Codex(exposure.workspace, state / "codex", exposure.executable)
        if exposure.workspace
        else Echo()
    )
    session_scope = hashlib.sha256(
        f"{exposure.runtime}\0{exposure.workspace}".encode()
    ).hexdigest()[:16]
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def run():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        with suppress(asyncio.CancelledError):
            if isinstance(runtime, Codex):
                await runtime.check_sandbox()
                await runtime.check_login()
            await Daemon(
                config.relay,
                config.token,
                config.agent,
                local_allowlist(ctx.obj, config),
                runtime,
                Sessions(state / f"sessions-{session_scope}.sqlite3"),
                exposure.timeout,
                exposure.concurrency,
            ).run()

    with local_lock(state / "daemon.lock"):
        asyncio.run(run())


def daemon_running(path: Path) -> bool:
    lock = state_path(path) / "daemon.lock"
    if not lock.is_file():
        return False
    with local_lock(lock, check_only=True) as running:
        return running


@daemon_app.command("status")
def daemon_status(ctx: typer.Context):
    """Show whether a daemon is running for this profile."""
    config = load(ctx.obj)
    state = "running" if daemon_running(ctx.obj) else "stopped"
    typer.echo(f"{config.agent} daemon is {state}.")


@app.command("agents")
def list_agents(ctx: typer.Context):
    """Show the current identity, runtime and trusted peers."""
    config = load(ctx.obj)
    show(
        {
            "agent": config.agent,
            "relay": config.relay,
            "runtime": config.exposure.runtime if config.exposure else None,
            "allowed": sorted(config.allowed),
        }
    )


@app.command()
def send(ctx: typer.Context, to: str, message: str, wait: Wait = 15, pr: str | None = None):
    """Ask a peer a question. Supply a PR as OWNER/REPO#NUMBER."""
    config = load(ctx.obj)
    context = None
    if pr is not None:
        repo, _, number = pr.rpartition("#")
        if not number.isascii() or not number.isdecimal() or len(number) > 10:
            raise PigeonError("invalid_pr", "Use --pr OWNER/REPO#NUMBER.")
        context = Context(type="github_pr", repo=repo, number=int(number))
    show(
        asyncio.run(
            ask_agent(config.relay, config.token, message, to=to, wait=wait, context=context)
        )
    )


@app.command()
def reply(ctx: typer.Context, conversation_id: UUID, message: str, wait: Wait = 15):
    """Send a follow-up after a completed turn."""
    config = load(ctx.obj)
    show(
        asyncio.run(
            ask_agent(
                config.relay, config.token, message, conversation_id=conversation_id, wait=wait
            )
        )
    )


@app.command()
def status(
    ctx: typer.Context,
    conversation_id: UUID | None = None,
    message_id: UUID | None = None,
):
    """List recent conversations or inspect one conversation."""
    if message_id and not conversation_id:
        raise PigeonError("missing_conversation", "Specify a conversation ID with --message-id.")
    query = (
        Inspect(conversation_id=conversation_id, message_id=message_id)
        if conversation_id
        else (ListCalls())
    )
    show(asyncio.run(rpc(load(ctx.obj), query)))


@app.command()
def cancel(ctx: typer.Context, conversation_id: UUID, message_id: UUID):
    """Cancel a request you sent."""
    show(
        asyncio.run(
            rpc(load(ctx.obj), Cancel(conversation_id=conversation_id, message_id=message_id))
        )
    )


@app.command()
def doctor(ctx: typer.Context):
    """Check local configuration, relay access and runtime prerequisites."""
    config = load(ctx.obj)
    remote = asyncio.run(rpc(config, Ping()))
    if remote["agent"] != config.agent:
        raise PigeonError("wrong_identity", "Relay credential belongs to a different agent.")
    typer.echo(f"Relay protocol {remote['protocol']} authenticated {config.agent}.")
    daemon_status(ctx)
    exposure = config.exposure
    if exposure and exposure.workspace:
        runtime = Codex(exposure.workspace, state_path(ctx.obj) / "codex", exposure.executable)
        asyncio.run(runtime.check_sandbox())
        typer.echo(f"Codex sandbox checks passed for {runtime.workspace}.")
        asyncio.run(runtime.check_login())
        typer.echo("Dedicated Codex home is signed in.")
    elif exposure is None:
        typer.echo("No inbound runtime configured.")


@app.command("mcp")
def mcp_server(ctx: typer.Context):
    """Run the local stdio MCP server."""
    from .mcp import create_mcp

    create_mcp(load(ctx.obj)).run(transport="stdio")


@app.command()
def install(ctx: typer.Context, host: Literal["claude"], scope: Literal["local", "user"] = "local"):
    """Register Pigeon with Claude Code using its MCP command."""
    load(ctx.obj)
    claude = shutil.which(host)
    if not claude:
        raise PigeonError("claude_missing", "Install Claude Code and put claude on your PATH.")
    result = subprocess.run(
        [
            claude,
            "mcp",
            "add",
            "--scope",
            scope,
            "--transport",
            "stdio",
            "pigeon",
            "--",
            sys.executable,
            "-m",
            "pigeon.cli",
            "--config",
            str(ctx.obj),
            "mcp",
        ],
        check=False,
    )
    if result.returncode:
        raise PigeonError("claude_install", "Claude Code could not register the Pigeon MCP server.")


def main():
    try:
        app()
    except PigeonError as exc:
        typer.echo(f"Error: {exc}", err=True)
        return 1
    except ValidationError as exc:
        typer.echo(
            f"Error: {exc.errors(include_input=False, include_url=False)[0]['msg']}", err=True
        )
        return 2
    except OSError as exc:
        typer.echo(f"Error: {exc.strerror or type(exc).__name__}", err=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
