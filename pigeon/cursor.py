"""Bind Cursor tool calls to the Cursor conversation that made them.

Cursor does not tell MCP servers which chat called a tool. Its preToolUse hook
does receive the conversation ID and can rewrite the tool input, so Pigeon's
hook adds a binding signed with a local key. The MCP server accepts only a
binding with a valid signature. The hook overwrites any binding the agent wrote.
"""

import hashlib
import hmac
import json
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path

from .config import config_lock, default_config, write_atomic
from .models import PigeonError

FIELD = "chat_binding"
TOOL_PREFIX = "MCP:pigeon_"
CONVERSATION = re.compile(r"[A-Za-z0-9-]{1,128}")
UNBOUND = (
    "Pigeon cannot identify this Cursor chat. Run pigeon setup cursor, reload Cursor, "
    "and retry. Pigeon will not use your account instead."
)
FORGED = (
    "This call's chat_binding was not signed by Pigeon's Cursor hook on this machine. "
    "Leave chat_binding unset. The hook fills it in."
)
# Recovery for a request already claimed when the hook stops running.
CLAIMED = (
    " To finish a request this chat already claimed, pass the claim token from "
    "pigeon_receive to pigeon_answer or pigeon_extend."
)
# Error codes for a call that arrived without a binding. A claim token can still
# authorize finishing that chat's claimed request.
UNBOUND_CODES = frozenset({"chat_unbound", "hook_restored", "hook_removed"})


def hooks_path() -> Path:
    return Path.home() / ".cursor" / "hooks.json"


def missing_binding(command: str | None, *, may_repair: bool) -> PigeonError:
    """Explain a call without a binding, restoring a removed hook if allowed."""
    path = hooks_path()
    if hook_installed(path):
        return PigeonError(
            "chat_unbound",
            "Pigeon's Cursor hook is installed but did not run for this call. "
            "Reload Cursor, then retry." + CLAIMED,
        )
    if command is None or not may_repair:
        # Rewriting a file another tool keeps resetting would only fight that tool.
        return PigeonError(
            "hook_removed",
            f"Pigeon's hook was removed from {path} again after Pigeon restored it, so "
            "Pigeon will not rewrite it. If a device-management tool manages that file, "
            "ask its owner to allow Pigeon's preToolUse hook." + CLAIMED,
        )
    try:
        register_hook(path, command)
    except PigeonError as exc:
        return PigeonError(
            "hook_removed",
            f"Pigeon's hook is missing from {path} and could not be restored: {exc} "
            "Run pigeon setup cursor." + CLAIMED,
        )
    return PigeonError(
        "hook_restored",
        f"Something removed Pigeon's hook from {path}. Pigeon restored it and kept your "
        "other hooks. Retry. Cursor's editor applies the hook at once. The Cursor CLI "
        "needs a new session." + CLAIMED,
    )


def key_path() -> Path:
    return default_config().parent / "cursor-hook.key"


def load_key(*, create: bool) -> bytes | None:
    path = key_path()
    if create and not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Several hooks can start at once. Publish a complete key with a link,
        # which fails if another hook already did, so no reader sees a partial file.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".cursor-hook-") as handle:
            handle.write(os.urandom(32))
            handle.flush()
            os.fsync(handle.fileno())
            with suppress(FileExistsError):
                os.link(handle.name, path)
    try:
        with path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_mode & 0o077:
                raise PigeonError("config_permissions", f"Run chmod 600 on {path}.")
            key = handle.read()
    except FileNotFoundError:
        return None
    return key if len(key) == 32 else None


def sign(conversation_id: str, key: bytes) -> str:
    digest = hmac.new(key, conversation_id.encode(), hashlib.sha256).hexdigest()
    return f"{conversation_id}.{digest}"


def verify(binding: str) -> str:
    conversation_id, _, _ = binding.rpartition(".")
    key = load_key(create=False)
    if (
        key is None
        or not CONVERSATION.fullmatch(conversation_id)
        or not hmac.compare_digest(sign(conversation_id, key), binding)
    ):
        raise PigeonError("chat_binding_invalid", FORGED)
    return conversation_id


def hook(event: dict) -> dict:
    """Answer one Cursor preToolUse event."""
    tool = event.get("tool_name")
    if event.get("hook_event_name") != "preToolUse" or not (
        isinstance(tool, str) and tool.startswith(TOOL_PREFIX)
    ):
        return {"permission": "allow"}
    conversation_id = event.get("conversation_id")
    arguments = event.get("tool_input")
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(conversation_id, str) or not CONVERSATION.fullmatch(conversation_id):
        return {"permission": "deny", "agent_message": UNBOUND, "user_message": UNBOUND}
    key = load_key(create=True)
    if key is None:
        raise PigeonError("chat_binding", f"Pigeon's Cursor key at {key_path()} is unreadable.")
    arguments = dict(arguments or {})
    arguments[FIELD] = sign(conversation_id, key)
    # Allow only continues to Cursor's own MCP approval. It does not approve the call.
    return {"permission": "allow", "updated_input": arguments}


def hook_entry(command: str) -> dict:
    return {
        "command": command,
        "matcher": "^" + TOOL_PREFIX,
        "failClosed": True,
        "timeout": 15,
    }


def register_hook(path: Path, command: str):
    with config_lock(path):
        original = path.read_bytes() if path.exists() else None
        try:
            settings = json.loads(original) if original is not None else {"version": 1}
        except ValueError:
            raise PigeonError("invalid_config", f"Fix the JSON in {path}, then retry.") from None
        hooks = settings.get("hooks", {}) if isinstance(settings, dict) else None
        entries = hooks.get("preToolUse", []) if isinstance(hooks, dict) else None
        if not isinstance(entries, list):
            raise PigeonError("invalid_config", f"Fix the hook configuration in {path}.")
        entry = hook_entry(command)
        # Replace an earlier Pigeon entry, such as one from a previous install path.
        kept = [item for item in entries if not is_pigeon_hook(item)]
        if kept + [entry] == entries:
            return
        settings.setdefault("version", 1)
        settings.setdefault("hooks", {})["preToolUse"] = kept + [entry]
        # Another tool may write without Pigeon's lock. Do not replace its newer copy.
        if (path.read_bytes() if path.exists() else None) != original:
            raise PigeonError("invalid_config", f"{path} changed while Pigeon was updating it.")
        write_atomic(path, json.dumps(settings, indent=2) + "\n")


def is_pigeon_hook(item) -> bool:
    return isinstance(item, dict) and str(item.get("command", "")).endswith(" cursor-hook")


def hook_installed(path: Path) -> bool:
    try:
        entries = json.loads(path.read_text())["hooks"]["preToolUse"]
        return any(is_pigeon_hook(item) for item in entries)
    except (OSError, ValueError, KeyError, TypeError):
        return False
