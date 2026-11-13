import asyncio
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag
from urllib.request import Request, urlopen
from uuid import UUID

import typer
from pydantic import ValidationError

from .adapters import Claude, Codex
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
from .db import open_store
from .models import (
    Cancel,
    Command,
    Context,
    Grant,
    Inspect,
    ListCalls,
    PigeonError,
    Ping,
    Signup,
    address,
)
from .server import serve

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
relay_app = typer.Typer(no_args_is_help=True, help="Run a relay and manage its agent credentials.")
app.add_typer(relay_app, name="relay")

Database = Annotated[Path, typer.Option("--db", help="Relay SQLite database.")]
Wait = Annotated[int, typer.Option(min=0, max=60, help="Seconds to wait for a response.")]
HOSTED_RELAY = "wss://pigeon-relay-rust.vercel.app"


@app.command()
def update():
    """Install the latest Pigeon and keep your account and chat state."""
    uv = shutil.which("uv")
    if uv is None:
        raise PigeonError("uv_missing", "Install uv and put it on your PATH.")
    result = subprocess.run(
        [uv, "tool", "install", "--reinstall", "git+https://github.com/zayedu/pigeon.git"],
    )
    if result.returncode:
        raise typer.Exit(result.returncode)
    typer.echo("Reopen your agent to load the update.")


@app.callback()
def root(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option(envvar="PIGEON_CONFIG")] = None,
):
    """Let your agent talk to mine."""
    ctx.obj = (config or default_config()).expanduser().absolute()
    ctx.meta["explicit_config"] = config is not None


def setup_profile(ctx: typer.Context, relay: str = HOSTED_RELAY) -> Path:
    path = ctx.obj
    if not ctx.meta["explicit_config"] and not path.exists() and relay == HOSTED_RELAY:
        legacy = path.with_name("hosted-codex.json")
        if legacy.is_file() and load(legacy).relay == HOSTED_RELAY:
            return legacy
    return path


@relay_app.command("serve")
def start_relay(
    db: Database,
    host: str = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    cert: Path | None = None,
    key: Path | None = None,
    behind_tls_proxy: bool = typer.Option(False, help="Let your hosting proxy terminate TLS."),
):
    """Start the relay in the foreground."""
    serve(db, host, port, cert, key, behind_tls_proxy=behind_tls_proxy)


@relay_app.command()
def enroll(
    agent: str, db: Annotated[Path | None, typer.Option("--db")] = None, rotate: bool = False
):
    """Issue a token for an agent. Share it privately."""
    typer.echo(open_store(db).enroll(agent, rotate=rotate))


@relay_app.command()
def invite(agent: str, relay: str, db: Annotated[Path | None, typer.Option("--db")] = None):
    """Create an address and print a private invite to share with its owner."""
    url = relay_endpoint(relay).removesuffix("/v1/ws")
    token = open_store(db).enroll(agent)
    typer.echo(f"{url}#{token}")


@relay_app.command("revoke")
def revoke_token(agent: str, db: Annotated[Path | None, typer.Option("--db")] = None):
    """Disable an agent's token and cancel its active turns."""
    open_store(db).revoke(address.validate_python(agent))
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
    connect_profile(ctx.obj, url, token)


def connect_profile(path: Path, url: str, token: str, *, session_profile: bool = False) -> Path:
    validate_token(token)

    async def identify():
        async with open_client(url, token) as client:
            return (await client.call(Ping()))["agent"]

    agent = address.validate_python(asyncio.run(identify()))
    if session_profile:
        owner, name = agent[1:].split("/")
        path = path.parent / "sessions" / owner / f"{name}.json"
    with config_lock(path):
        if path.exists():
            config = load(path)
            if (config.relay, config.agent) != (url, agent):
                raise PigeonError(
                    "different_identity", "Use a different --config for a new relay or agent."
                )
            config = config.model_copy(update={"token": token})
        else:
            config = Config(relay=url, agent=agent, token=token)
        save(path, config)
    typer.echo(f"Connected as {agent}.")
    return path


@app.command()
def setup(
    ctx: typer.Context,
    client: Literal["codex", "claude", "cursor"],
    username: str | None = None,
    relay: str = HOSTED_RELAY,
):
    """Create your Pigeon account and register it with your coding agent."""
    if client != "cursor" and not shutil.which(client):
        raise PigeonError("client_missing", f"Install {client} and put it on your PATH.")
    ctx.obj = setup_profile(ctx, relay)
    with config_lock(ctx.obj):
        if not ctx.obj.exists():
            url = relay_endpoint(relay).removesuffix("/v1/ws")
            payload = Signup(
                username=(username or typer.prompt("Choose a username")).strip().lower()
            )
            endpoint = ("https" if url.startswith("wss:") else "http") + url[url.index(":") :]
            request = Request(
                endpoint + "/v1/signup",
                data=payload.model_dump_json().encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urlopen(request, timeout=15) as response:
                    account = json.load(response)
            except HTTPError as exc:
                if exc.code == 409:
                    raise PigeonError(
                        "username_taken", "That username is taken. Try another."
                    ) from None
                raise PigeonError(
                    "signup_failed", f"Relay signup failed (HTTP {exc.code})."
                ) from None
            except (URLError, TimeoutError):
                raise PigeonError("signup_failed", "Could not reach the signup endpoint.") from None
            config = Config(relay=url, **account)
            save(ctx.obj, config)
            typer.echo(f"Created {config.agent}.")
        else:
            username = load(ctx.obj).agent.removeprefix("@").split("/", 1)[0]
            typer.echo(f"Using your Pigeon account @{username}.")
    register_mcp(ctx.obj, client, "user", name="pigeon-relay")
    install_skill(client)


def install_skill(client: str):
    skill_root = Path.home() / ".agents"
    if client == "claude":
        skill_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    path = skill_root / "skills" / "pigeon" / "SKILL.md"
    with config_lock(path):
        if not path.exists():
            content = files("pigeon").joinpath("skills/pigeon/SKILL.md").read_text(encoding="utf-8")
            path.write_text(content, encoding="utf-8")


@app.command()
def join(
    ctx: typer.Context,
    client: Literal["claude", "codex"],
    session: bool = False,
    allow: str | None = None,
):
    """Join from an invite and connect your coding agent."""
    relay, token = urldefrag(typer.prompt("Paste your Pigeon invite", hide_input=True).strip())
    url = relay_endpoint(relay).removesuffix("/v1/ws")
    if session and not allow:
        allow = typer.prompt("Reviewer's Pigeon address")
    if allow:
        address.validate_python(allow)
    ctx.obj = connect_profile(
        ctx.obj, url, token, session_profile=session and ctx.obj == default_config().absolute()
    )
    if allow:
        set_permission(ctx.obj, allow, True)
    if session:
        open_session(ctx, client)
    else:
        register_mcp(ctx.obj, client, "local", name="pigeon-relay")


@app.command()
def wake(
    ctx: typer.Context,
    client_or_thread: str,
    session_id: Annotated[str | None, typer.Argument()] = None,
):
    """Open a wake-enabled chat in Codex, Claude Code, or Cursor."""
    ctx.obj = setup_profile(ctx)
    load(ctx.obj)
    if client_or_thread == "cursor":
        from .cursor_acp import run_cursor

        binary = shutil.which("agent") or shutil.which("cursor-agent")
        if not binary:
            raise PigeonError("client_missing", "Install and sign in to Cursor CLI first.")
        asyncio.run(run_cursor(binary, ctx.obj, session_id))
        return
    if client_or_thread == "claude":
        binary = shutil.which("claude")
        if not binary:
            raise PigeonError("client_missing", "Install and sign in to Claude Code first.")
        args = ["-m", "pigeon.cli", "--config", str(ctx.obj), "mcp", "--claude-channel"]
        config = {"mcpServers": {"pigeon-relay": {"command": sys.executable, "args": args}}}
        launch = [
            binary,
            "--mcp-config",
            json.dumps(config),
            "--dangerously-load-development-channels",
            "server:pigeon-relay",
        ]
        if session_id:
            launch.extend(["--resume", session_id])
        raise typer.Exit(subprocess.call(launch))
    try:
        if session_id is not None and client_or_thread != "codex":
            raise ValueError
        thread = UUID((session_id or "") if client_or_thread == "codex" else client_or_thread)
    except ValueError:
        raise typer.BadParameter(
            "Use a Codex thread UUID, claude [session ID], or cursor [session ID]."
        ) from None
    lock = default_config().expanduser().parent / "wake" / f"{thread}.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with local_lock(lock, check_only=True) as already_open:
        if already_open:
            raise PigeonError(
                "locked", "This chat is already open with pigeon wake. Use that terminal."
            )
        wake_codex(ctx.obj, thread)


def wake_codex(profile: Path, thread: UUID):
    binary = shutil.which("codex")
    if not binary:
        raise PigeonError("client_missing", "Install Codex and put it on your PATH.")
    typer.echo(
        "If Codex opens read-only, exit the same chat in the other app or terminal "
        "and press r here."
    )
    # Unix socket paths are limited to about 100 bytes on macOS.
    with tempfile.TemporaryDirectory(prefix="pigeon-", dir="/tmp") as directory:
        socket = Path(directory) / "codex.sock"
        endpoint = f"unix://{socket}"
        args = [
            "-m",
            "pigeon.cli",
            "--config",
            str(profile),
            "mcp",
            "--codex-thread",
            str(thread),
            "--codex-bin",
            str(Path(binary).resolve()),
            "--codex-endpoint",
            endpoint,
        ]
        mcp_config = (
            f"mcp_servers.pigeon-relay={{command={json.dumps(sys.executable)},"
            f"args={json.dumps(args)}}}"
        )
        with subprocess.Popen(
            [binary, "-c", mcp_config, "app-server", "--listen", endpoint],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            start_new_session=True,
        ) as server:
            try:
                deadline = time.monotonic() + 10
                while not socket.exists():
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise PigeonError("wake_failed", "Could not start the Codex app server.")
                    time.sleep(0.05)
                raise typer.Exit(
                    subprocess.call([binary, "resume", "--remote", endpoint, str(thread)])
                )
            finally:
                with suppress(ProcessLookupError):
                    server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


@app.command("session")
def open_session(
    ctx: typer.Context,
    client: Literal["claude", "codex"],
    session_id: Annotated[UUID | None, typer.Argument()] = None,
):
    """Open the session picker or resume by ID with the Pigeon inbox."""
    config = load(ctx.obj)
    if not config.allowed:
        raise PigeonError("peer_required", "Allow a reviewer before opening the session inbox.")
    binary = shutil.which(client)
    if not binary:
        raise PigeonError("client_missing", f"Install {client} and put it on your PATH.")
    args = ["-m", "pigeon.cli", "--config", str(ctx.obj), "mcp", "--session"]
    if client == "codex":
        server = (
            f"mcp_servers.pigeon-session={{command={json.dumps(sys.executable)},"
            f"args={json.dumps(args)}}}"
        )
        launch = [binary, "-c", server, "resume"]
    else:
        server = {"mcpServers": {"pigeon-session": {"command": sys.executable, "args": args}}}
        launch = [binary, "--mcp-config", json.dumps(server), "--resume"]
    if session_id is not None:
        launch.append(str(session_id))
    raise typer.Exit(subprocess.call(launch))


@app.command()
def expose(
    ctx: typer.Context,
    agent: Annotated[Literal["codex", "claude", "echo"], typer.Option("--agent")],
    repo: Path | None = None,
    timeout: Annotated[int, typer.Option(min=1, max=900)] = 120,
    concurrency: Annotated[int, typer.Option(min=1, max=4)] = 1,
    codex: str = "codex",
    claude: str = "claude",
):
    """Configure the runtime for this identity."""
    if (agent == "codex") != (repo is not None):
        raise PigeonError("invalid_workspace", "Supply --repo for Codex only.")
    workspace = repo.resolve() if repo else None
    state = state_path(ctx.obj)
    runtime = None
    with config_lock(ctx.obj):
        config = load(ctx.obj)
        if daemon_running(ctx.obj):
            raise PigeonError("daemon_running", "Stop the daemon before changing its runtime.")
        if workspace:
            runtime = Codex(workspace, state / "codex", codex)
        elif agent == "claude":
            runtime = Claude(state / "claude", claude)
        config.exposure = Exposure(
            runtime=agent,
            workspace=workspace,
            executable=runtime.binary if runtime else "echo",
            timeout=timeout,
            concurrency=concurrency,
        )
        save(ctx.obj, config)
    typer.echo(f"Configured {config.agent} to use {agent}.")
    if isinstance(runtime, Codex):
        typer.echo("Sign in to the dedicated Codex home if needed.")
        typer.echo(
            f"CODEX_HOME={shlex.quote(str(runtime.home))} "
            + shlex.join([runtime.binary, "-c", 'cli_auth_credentials_store="file"', "login"])
        )
    elif isinstance(runtime, Claude):
        typer.echo("Sign in to the dedicated Claude profile if needed.")
        typer.echo(
            f"CLAUDE_CONFIG_DIR={shlex.quote(str(runtime.home))} "
            + shlex.join([runtime.binary, "auth", "login"])
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
    if exposure.workspace:
        runtime = Codex(exposure.workspace, state / "codex", exposure.executable)
    elif exposure.runtime == "claude":
        runtime = Claude(state / "claude", exposure.executable)
    else:
        runtime = Echo()
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
            if isinstance(runtime, (Claude, Codex)):
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
    elif exposure and exposure.runtime == "claude":
        runtime = Claude(state_path(ctx.obj) / "claude", exposure.executable)
        asyncio.run(runtime.check_login())
        typer.echo("Dedicated Claude profile is signed in.")
    elif exposure is None:
        typer.echo("No inbound runtime configured.")


@app.command("mcp")
def mcp_server(
    ctx: typer.Context,
    session: bool = False,
    codex_thread: UUID | None = None,
    codex_bin: Path | None = None,
    codex_endpoint: str | None = None,
    claude_channel: bool = False,
    cursor_endpoint: str | None = None,
):
    """Run the local stdio MCP server."""
    from contextlib import ExitStack

    from .mcp import create_mcp

    config = load(ctx.obj)
    wake_options = (codex_bin, codex_thread, codex_endpoint)
    targets = (any(wake_options), claude_channel, cursor_endpoint is not None)
    if (any(wake_options) and not all(wake_options)) or sum(targets) + session > 1:
        raise PigeonError("wake_config", "Use pigeon wake to launch automatic delivery.")
    with ExitStack() as stack:
        lock = None
        if session:
            state = state_path(ctx.obj)
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = state / "daemon.lock"
        create_mcp(
            config,
            allowed=local_allowlist(ctx.obj, config) if session else None,
            claim_session=(lambda: stack.enter_context(local_lock(lock))) if lock else None,
            codex_wake=(str(codex_bin), codex_thread, codex_endpoint)
            if codex_bin and codex_thread and codex_endpoint
            else None,
            claude_channel=claude_channel,
            cursor_endpoint=cursor_endpoint,
            profile=ctx.obj,
        ).run(transport="stdio")


@app.command()
def install(
    ctx: typer.Context,
    host: Literal["claude", "codex", "cursor"],
    scope: Literal["local", "user"] = "local",
):
    """Register Pigeon with Codex, Claude Code, or Cursor."""
    register_mcp(ctx.obj, host, scope, name="pigeon")


def register_mcp(path: Path, host: str, scope: str, *, name: str):
    load(path)
    command = [sys.executable, "-m", "pigeon.cli", "--config", str(path), "mcp"]
    if host == "cursor":
        register_cursor(Path.home() / ".cursor" / "mcp.json", name, command)
        typer.echo("Reload Cursor to load Pigeon.")
        return
    binary = shutil.which(host)
    if not binary:
        raise PigeonError("client_missing", f"Install {host} and put it on your PATH.")
    existing = subprocess.run(
        [binary, "mcp", "get", name], capture_output=True, text=True, check=False
    )
    if existing.returncode == 0:
        if str(path) in existing.stdout and "pigeon.cli" in existing.stdout:
            typer.echo(f"Restart {host} to load {name}.")
            return
        raise PigeonError(
            "already_installed", f"{name} is already registered. Check `{host} mcp get {name}`."
        )
    args = [binary, "mcp", "add"]
    if host == "claude":
        args.extend(["--scope", scope, "--transport", "stdio"])
    result = subprocess.run([*args, name, "--", *command], check=False)
    if result.returncode:
        raise PigeonError("install_failed", f"{host} could not register the Pigeon MCP server.")
    typer.echo(f"Restart {host} to load {name}.")


def register_cursor(path: Path, name: str, command: list[str]):
    with config_lock(path):
        try:
            settings = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            raise PigeonError("invalid_config", f"Fix the JSON in {path}, then retry.") from None
        if not isinstance(settings, dict) or not isinstance(settings.get("mcpServers", {}), dict):
            raise PigeonError("invalid_config", f"Fix the MCP server configuration in {path}.")
        servers = settings.setdefault("mcpServers", {})
        entry = {"command": command[0], "args": command[1:]}
        if name in servers:
            if servers[name] == entry:
                return
            raise PigeonError("already_installed", f"{name} is already registered in {path}.")
        servers[name] = entry
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(json.dumps(settings, indent=2) + "\n")
                handle.flush()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)


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
