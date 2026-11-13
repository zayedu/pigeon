import hashlib
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, model_validator

from .models import Address, PigeonError, Share


def default_config() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "pigeon" / "config.json"


class Exposure(BaseModel):
    runtime: Literal["echo", "codex", "claude"]
    workspace: Path | None = None
    executable: str = "codex"
    timeout: Annotated[int, Field(ge=1, le=900)] = 120
    concurrency: Annotated[int, Field(ge=1, le=4)] = 1

    @model_validator(mode="after")
    def check_workspace(self):
        if (self.runtime == "codex") != (self.workspace is not None):
            raise ValueError("A workspace is required for Codex only.")
        return self


class Config(BaseModel):
    relay: str
    token: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)]
    agent: Address
    allowed: frozenset[Address] = frozenset()
    exposure: Exposure | None = None


def load(path: Path) -> Config:
    try:
        with path.open() as handle:
            if os.fstat(handle.fileno()).st_mode & 0o077:
                raise PigeonError("config_permissions", f"Run chmod 600 on {path}.")
            return Config.model_validate_json(handle.read())
    except FileNotFoundError:
        raise PigeonError("not_configured", f"Run pigeon login to create {path}.") from None
    except ValidationError:
        raise PigeonError("invalid_config", f"Invalid Pigeon configuration at {path}.") from None


def save(path: Path, config: Config):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(config.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def validate_token(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise PigeonError("invalid_token", "Paste the token issued by pigeon relay enroll.")


def state_path(path: Path) -> Path:
    return path.with_name(path.name + ".state")


def chat_share(profile: Path | None, config: Config, chat_id: str | None) -> Share:
    if profile is None or chat_id is None:
        return Share(session_id=uuid4(), session_token=secrets.token_urlsafe(32))
    identity = hashlib.sha256(f"{config.relay}\n{config.agent}\n{chat_id}".encode()).hexdigest()
    path = state_path(profile) / "chats" / f"{identity}.json"
    with config_lock(path):
        if path.exists():
            return Share.model_validate_json(path.read_bytes())
        share = Share(session_id=uuid4(), session_token=secrets.token_urlsafe(32))
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(share.model_dump_json().encode())
                handle.flush()
                os.fsync(handle.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return share


@contextmanager
def config_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with local_lock(path.with_name(path.name + ".lock")):
        yield


@contextmanager
def local_lock(path: Path, *, check_only: bool = False):
    import fcntl

    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if check_only:
                yield True
                return
            raise PigeonError("locked", f"Another Pigeon process is using {path.name}.") from None
        yield False


def local_allowlist(path: Path, initial: Config) -> Callable[[], frozenset[str]]:
    def allowed():
        config = load(path)
        if (config.agent, config.relay, config.exposure) != (
            initial.agent,
            initial.relay,
            initial.exposure,
        ):
            return frozenset()
        return config.allowed

    return allowed
