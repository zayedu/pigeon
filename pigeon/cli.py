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
from .attachments import download_file, upload_file
from .client import ask_agent, open_client, relay_endpoint
from .config import (
    Config,
    Exposure,
    chat_share,
    config_lock,
    default_config,
    load,
    local_allowlist,
    local_lock,
    save,
    state_path,
    validate_token,
    write_atomic,
)
from .daemon import Daemon, Echo, Sessions
from .db import open_store
from .models import (
    ACTIVE,
    MAX_WAIT,
    Cancel,
    Capabilities,
    Close,
    Command,
    Context,
    Finish,
    Grant,
    History,
    Inspect,
    ListCalls,
    PigeonError,
    Ping,
    Redeem,
    Renew,
    Signup,
    Status,
    Take,
    Turn,
    address,
    message_problem,
)
from .server import serve

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
relay_app = typer.Typer(no_args_is_help=True, help="Run a relay and manage its agent credentials.")
app.add_typer(relay_app, name="relay")
attachment_app = typer.Typer(no_args_is_help=True, help="Transfer a file from your terminal.")
app.add_typer(attachment_app, name="attachment")

Database = Annotated[Path, typer.Option("--db", help="Relay SQLite database.")]
Wait = Annotated[int, typer.Option(min=0, max=600, help="Seconds to wait for a response.")]
Chat = Annotated[
    str | None,
    typer.Option(help="Act as this chat's inbox, e.g. codex:<thread-id> or cursor:<chat-id>."),
]
RequiredChat = Annotated[
    str, typer.Option(help="Act as this chat's inbox, e.g. codex:<thread-id> or cursor:<chat-id>.")
]
Message = Annotated[str, typer.Argument(help="Message text, or - to read it from stdin.")]
HOSTED_RELAY = "wss://pigeon-relay-rust.vercel.app"
HOMEBREW_LAUNCHER_ENV = "PIGEON_HOMEBREW_LAUNCHER"
# Hashes of stock skills shipped before installations recorded their own digest.
LEGACY_SKILL_DIGESTS = {
    "1c938188b9e398f1085f5de06fa7cfbea3816a9f7f93c3b36b91eec8573c6e08",
    "e03db3e0cee16f0d9fd2dc5d13aef9e17c5a49fcc8651fdf2b559175e9ac62f2",
    "ef67a83b600adbd2fb8bb38dea5a41c9ed76c2b836c6efafc99bbed6a9cb5b9c",
    "abfaa727f3394f3d9ad2eda78456be8b6efae6c5a0ee56c44f593093e516a15f",
    "5698da69ea03528b35d27ebf8a2f6cd4efdf63c43553e84c4bb9db35a31f0112",
    "71944c1499c5d43a37c6bb40c5d2f73a055dd8ce56d51ab73ec3448c088ecf2c",
    "b6b27eb586b91d965809e035bf5f758c2d60dadd1e196b8e9861b1eed2d72887",
}


@app.command()
def update():
    """Install the latest Pigeon and keep your account and chat state."""
    if os.environ.get(HOMEBREW_LAUNCHER_ENV):
        typer.echo("Run brew upgrade pigeon, then rerun pigeon setup for your client.")
        return
    uv = shutil.which("uv")
    if uv is None:
        raise PigeonError("uv_missing", "Install uv and put it on your PATH.")
    result = subprocess.run(
        [uv, "tool", "install", "--reinstall", "git+https://github.com/zayedu/pigeon.git"],
    )
    if result.returncode:
        raise typer.Exit(result.returncode)
    result = subprocess.run([sys.executable, "-I", "-m", "pigeon.cli", "refresh-skills"])
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


@relay_app.command("prune-attachments")
def prune_attachments(db: Annotated[Path | None, typer.Option("--db")] = None):
    """Delete expired attachment bytes from a local or configured hosted database."""
    open_store(db).prune_attachments()


async def rpc(config: Config, command: Command):
    async with open_client(config.relay, config.token) as client:
        return await client.call(command)


def inspect_profile(path: Path, chat: str | None, *, create: bool = False) -> Config:
    """Return the account, or the inbox of one chat. Never substitutes one for the other."""
    config = load(path)
    if chat is None:
        return config
    if chat.partition(":")[0] not in {"codex", "claude", "cursor", "antigravity"}:
        raise PigeonError("chat_id", "Use a chat ID such as codex:<thread-id>.")
    identity = chat_share(path, config, chat, existing_only=not create)
    try:
        result = asyncio.run(rpc(config, identity.model_copy(update={"resume": True})))
    except PigeonError as exc:
        if not create or exc.code != "not_found":
            raise
        result = asyncio.run(rpc(config, identity))
    return Config(relay=config.relay, agent=result["address"], token=result["token"])


def read_message(message: str) -> str:
    if message == "-":
        message = sys.stdin.read().removesuffix("\n")
    if problem := message_problem(message):
        raise PigeonError("message_limit", problem)
    return message


def confirm_transfer(action: str, path: Path, conversation_id: UUID) -> Path:
    if not sys.stdin.isatty():
        raise PigeonError(
            "approval_required", "Run this transfer in your own interactive terminal."
        )
    path = path.expanduser()
    try:
        path = path.parent.resolve(strict=True) / path.name
    except OSError:
        raise PigeonError("file_path", "Choose a file in an existing directory.") from None
    typer.echo(f"{action} {str(path)!r} in session {conversation_id}.")
    typer.confirm("Authorize this transfer", abort=True)
    return path


@attachment_app.command("upload")
def attachment_upload(
    ctx: typer.Context, conversation_id: UUID, source: Path, chat: Annotated[str, typer.Option()]
):
    """Upload a file selected and confirmed by the local user."""
    source = confirm_transfer("Upload", source, conversation_id)
    show(upload_file(inspect_profile(setup_profile(ctx), chat), conversation_id, source))


@attachment_app.command("download")
def attachment_download(
    ctx: typer.Context,
    conversation_id: UUID,
    attachment_id: UUID,
    destination: Path,
    chat: Annotated[str, typer.Option()],
    sha256: str | None = None,
):
    """Save an attachment to a new file selected and confirmed by the local user."""
    destination = confirm_transfer("Download to", destination, conversation_id)
    actor = inspect_profile(setup_profile(ctx), chat)
    show(download_file(actor, conversation_id, attachment_id, destination, sha256))


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
    client: Literal["codex", "claude", "cursor", "antigravity"],
    username: str | None = None,
    relay: str = HOSTED_RELAY,
):
    """Create your Pigeon account and register it with your coding agent."""
    binary = "agy" if client == "antigravity" else client
    if client != "cursor" and not shutil.which(binary):
        raise PigeonError("client_missing", f"Install {binary} and put it on your PATH.")
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
    if client == "antigravity":
        typer.echo("Run pigeon antigravity to open a chat with Pigeon.")
    else:
        register_mcp(ctx.obj, client, "user", name="pigeon-relay")
    install_skill(client)


def pigeon_command() -> list[str]:
    launcher = os.environ.get(HOMEBREW_LAUNCHER_ENV)
    return [launcher] if launcher else [sys.executable, "-m", "pigeon.cli"]


def cursor_hooks_path() -> Path:
    return Path.home() / ".cursor" / "hooks.json"


def skill_path(client: str) -> Path:
    # Codex and Cursor both read ~/.agents/skills.
    skill_root = Path.home() / ".agents"
    if client == "claude":
        skill_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    elif client == "antigravity":
        skill_root = Path.home() / ".gemini" / "antigravity-cli"
    return skill_root / "skills" / "pigeon" / "SKILL.md"


def install_skill(client: str, *, existing_only: bool = False):
    path = skill_path(client)
    if existing_only and not path.is_file():
        return
    content = files("pigeon").joinpath("skills/pigeon/SKILL.md").read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    stamp = path.with_name(".pigeon-skill.sha256")
    with config_lock(path):
        if path.exists():
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            previous = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else None
            if current not in LEGACY_SKILL_DIGESTS and current not in (digest, previous):
                typer.echo(f"Kept your edited Pigeon skill at {path}.")
                return
        if not path.exists() or current != digest:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    handle.write(content)
                    handle.flush()
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        stamp.write_text(digest + "\n", encoding="utf-8")


SKILL_HOSTS = ("codex", "claude", "cursor", "antigravity")


@app.command(hidden=True)
def refresh_skills():
    """Refresh previously installed skills from this version."""
    for client in {skill_path(client): client for client in SKILL_HOSTS}.values():
        install_skill(client, existing_only=True)
    from . import cursor

    try:
        servers = json.loads((Path.home() / ".cursor" / "mcp.json").read_text())["mcpServers"]
        # Pigeon registers [..., "--config", PROFILE, "mcp"] with an optional "--cursor".
        registered = any(
            "--config" in item.get("args", []) and "mcp" in item.get("args", [])
            for item in servers.values()
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        registered = False
    if registered and not cursor.hook_installed(cursor_hooks_path()):
        typer.echo("Run pigeon setup cursor again so Cursor chats can use Pigeon.")


@app.command("cursor-hook", hidden=True)
def cursor_hook():
    """Answer a Cursor preToolUse hook event on stdin."""
    from . import cursor

    try:
        result = cursor.hook(json.load(sys.stdin))
    except (PigeonError, ValueError) as exc:
        reason = f"Pigeon's Cursor hook failed: {exc}"
        result = {"permission": "deny", "agent_message": reason, "user_message": reason}
    typer.echo(json.dumps(result))


@app.command()
def antigravity(ctx: typer.Context, session_id: Annotated[str | None, typer.Argument()] = None):
    """Open an Antigravity CLI chat with Pigeon registered for that chat."""
    from .acp import run_acp

    ctx.obj = setup_profile(ctx)
    load(ctx.obj)
    binary = shutil.which("agy")
    if not binary:
        raise PigeonError("client_missing", "Install Antigravity CLI and sign in with agy.")
    asyncio.run(run_acp(binary, ctx.obj, session_id, antigravity=True))


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
        from .acp import run_acp

        binary = shutil.which("agent") or shutil.which("cursor-agent")
        if not binary:
            raise PigeonError("client_missing", "Install and sign in to Cursor CLI first.")
        asyncio.run(run_acp(binary, ctx.obj, session_id))
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
        raise PigeonError("peer_required", "Allow a peer before opening the session inbox.")
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
    """Revoke a peer's relay grant. Invitation sessions stay open."""
    set_permission(ctx.obj, sender, False)
    typer.echo(f"Revoked the grant for {sender}. Invitation sessions stay open.")


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
def reply(
    ctx: typer.Context, conversation_id: UUID, message: Message, wait: Wait = 15, chat: Chat = None
):
    """Send the next message in a session after the previous turn ends."""
    actor = inspect_profile(setup_profile(ctx), chat)
    text = read_message(message)
    show(
        asyncio.run(
            ask_agent(actor.relay, actor.token, text, conversation_id=conversation_id, wait=wait)
        )
    )


@app.command()
def status(
    ctx: typer.Context,
    conversation_id: UUID | None = None,
    message_id: UUID | None = None,
    chat: Chat = None,
    pending: Annotated[bool, typer.Option(help="List only pending requests.")] = False,
    wait: Annotated[
        int, typer.Option(min=0, max=600, help="Seconds to wait for the request to change.")
    ] = 0,
    after: Annotated[
        Status | None, typer.Option(help="Wait while the request is in this state.")
    ] = None,
):
    """List recent conversations, or inspect and optionally wait on one request."""
    if message_id and not conversation_id:
        raise PigeonError("missing_conversation", "Specify a conversation ID with --message-id.")
    if pending and conversation_id:
        raise PigeonError("invalid_option", "Use --pending without a conversation ID.")
    actor = inspect_profile(setup_profile(ctx), chat)
    if conversation_id is None:
        result = asyncio.run(rpc(actor, ListCalls()))
        show([row for row in result if row["status"] in ACTIVE] if pending else result)
        return

    async def inspect():
        async with open_client(actor.relay, actor.token) as client:
            turn = await client.turn(
                Inspect(conversation_id=conversation_id, message_id=message_id)
            )
            if wait and (after is None or turn.status == after):
                turn = await client.wait_for(turn, wait, until_terminal=False)
            return turn

    show(asyncio.run(inspect()))


@app.command()
def history(
    ctx: typer.Context,
    conversation_id: UUID,
    chat: Chat = None,
    limit: Annotated[int, typer.Option(min=1, max=50)] = 20,
    before: Annotated[
        int | None, typer.Option(min=1, help="Cursor from the previous page.")
    ] = None,
):
    """Show earlier turns in a session, newest first."""
    actor = inspect_profile(setup_profile(ctx), chat)
    show(
        asyncio.run(
            rpc(actor, History(conversation_id=conversation_id, limit=limit, before=before))
        )
    )


@app.command()
def connect(ctx: typer.Context, key: str, chat: RequiredChat):
    """Join a Pigeon session key as one chat."""
    join = Redeem(key=key.strip(), peer_session=True)
    actor = inspect_profile(setup_profile(ctx), chat, create=True)
    show(asyncio.run(rpc(actor, join)))


@app.command()
def receive(ctx: typer.Context, chat: RequiredChat, wait: Wait = 0):
    """Claim the next queued request for a chat. Prints null when none arrives."""
    actor = inspect_profile(setup_profile(ctx), chat)

    async def take():
        until = time.monotonic() + wait
        async with open_client(actor.relay, actor.token) as client:
            while True:
                remaining = round(until - time.monotonic())
                result = await client.call(Take(wait=max(0, min(MAX_WAIT, remaining))))
                if result or remaining <= MAX_WAIT:
                    return result

    result = asyncio.run(take())
    show(Turn.model_validate(result) if result else None)


@app.command()
def answer(ctx: typer.Context, message_id: UUID, message: Message, chat: RequiredChat):
    """Answer a request this chat claimed."""
    actor = inspect_profile(setup_profile(ctx), chat)
    text = read_message(message)
    show(asyncio.run(rpc(actor, Finish(message_id=message_id, status="completed", content=text))))


@app.command()
def extend(ctx: typer.Context, message_id: UUID, chat: RequiredChat):
    """Extend the deadline of a request this chat claimed and is still working on."""
    show(asyncio.run(rpc(inspect_profile(setup_profile(ctx), chat), Renew(message_id=message_id))))


@app.command()
def cancel(ctx: typer.Context, conversation_id: UUID, message_id: UUID, chat: Chat = None):
    """Cancel a request you sent."""
    actor = inspect_profile(setup_profile(ctx), chat)
    show(asyncio.run(rpc(actor, Cancel(conversation_id=conversation_id, message_id=message_id))))


@app.command()
def close(ctx: typer.Context, conversation_id: UUID, chat: Chat = None):
    """Close one invitation session. Its history stays readable."""
    actor = inspect_profile(setup_profile(ctx), chat)
    show(asyncio.run(rpc(actor, Close(conversation_id=conversation_id))))


@app.command()
def doctor(ctx: typer.Context, chat: str | None = None):
    """Check local configuration, relay access and runtime prerequisites."""
    ctx.obj = setup_profile(ctx)
    config = inspect_profile(ctx.obj, chat)
    remote = asyncio.run(rpc(config, Ping()))
    if remote["agent"] != config.agent:
        raise PigeonError("wrong_identity", "Relay credential belongs to a different agent.")
    typer.echo(f"Relay protocol {remote['protocol']} authenticated {config.agent}.")
    typer.echo(f"Relay features: {', '.join(remote.get('features', [])) or 'baseline only'}.")
    if chat:
        if "peer_capabilities" in remote.get("features", []):
            result = asyncio.run(rpc(config, Capabilities()))
            features = ", ".join(result["features"] or [])
            typer.echo(f"Chat capabilities: {features or 'unavailable'}.")
            if result["features"] is not None:
                mode = "listener advertised" if "wake" in result["features"] else "manual inbox"
                typer.echo(f"Wake mode: {mode}.")
        typer.echo("Run pigeon_status in the open chat for live wake diagnostics.")
        return
    integrations = [
        label
        for command, label in (
            ("codex", "Codex"),
            ("claude", "Claude Code"),
            ("cursor-agent", "Cursor CLI"),
            ("cursor", "Cursor"),
            ("agy", "Antigravity"),
        )
        if shutil.which(command)
    ]
    typer.echo(f"Agent executables on PATH: {', '.join(integrations) or 'none'}.")
    installed = [host for host in SKILL_HOSTS if skill_path(host).is_file()]
    typer.echo(f"Pigeon skills installed for: {', '.join(installed) or 'none'}.")
    if (Path.home() / ".cursor").is_dir():
        from . import cursor

        ready = cursor.hook_installed(cursor_hooks_path()) and cursor.load_key(create=False)
        typer.echo(
            "Cursor chat identity hook: "
            + ("installed." if ready else "missing. Run pigeon setup cursor and reload Cursor.")
        )
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
    cursor: Annotated[bool, typer.Option(help="Identify chats with Pigeon's Cursor hook.")] = False,
    antigravity_session_file: Path | None = None,
):
    """Run the local stdio MCP server."""
    from contextlib import ExitStack

    from .mcp import create_mcp

    config = load(ctx.obj)
    wake_options = (codex_bin, codex_thread, codex_endpoint)
    targets = (
        any(wake_options),
        claude_channel,
        cursor_endpoint is not None,
        cursor,
        antigravity_session_file is not None,
    )
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
            cursor_chat=cursor,
            antigravity_session_file=antigravity_session_file,
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
    launcher = os.environ.get(HOMEBREW_LAUNCHER_ENV)
    command = [*pigeon_command(), "--config", str(path), "mcp"]
    if host == "cursor":
        from . import cursor

        register_cursor(Path.home() / ".cursor" / "mcp.json", name, [*command, "--cursor"])
        cursor.load_key(create=True)
        cursor.register_hook(cursor_hooks_path(), shlex.join([*pigeon_command(), "cursor-hook"]))
        typer.echo("Reload Cursor to load Pigeon and its chat identity hook.")
        return
    binary = shutil.which(host)
    if not binary:
        raise PigeonError("client_missing", f"Install {host} and put it on your PATH.")
    existing = subprocess.run(
        [binary, "mcp", "get", name], capture_output=True, text=True, check=False
    )
    if existing.returncode == 0:
        if str(path) in existing.stdout and (launcher or "pigeon.cli") in existing.stdout:
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
            # Upgrade the entry an earlier Pigeon wrote before hook identity existed.
            if servers[name] != {**entry, "args": entry["args"][:-1]}:
                raise PigeonError("already_installed", f"{name} is already registered in {path}.")
        servers[name] = entry
        write_atomic(path, json.dumps(settings, indent=2) + "\n")


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
