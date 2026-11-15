import hashlib
import json
import os
import stat
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from .client import relay_endpoint
from .config import Config
from .models import MAX_ATTACHMENT, PigeonError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def transfer(config: Config, path: str, *, data: bytes | None = None):
    relay = relay_endpoint(config.relay).removesuffix("/v1/ws")
    url = ("https" if relay.startswith("wss:") else "http") + relay[relay.index(":") :]
    request = Request(
        url + path,
        data=data,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            payload = response.read(MAX_ATTACHMENT + 1)
            return payload, response.headers
    except HTTPError as exc:
        code = exc.code
        exc.close()
        reason = {
            404: "Open session or attachment not found.",
            409: "Peer attachment support is unavailable.",
            413: "Attachment exceeds 1 MiB.",
            429: "Relay transfer limit reached. Try again later.",
        }.get(code, f"Attachment transfer failed (HTTP {code}).")
        raise PigeonError("transfer", reason) from None
    except (URLError, OSError, TimeoutError) as exc:
        raise PigeonError("transfer", "Could not reach the attachment endpoint.") from exc


def upload_file(config: Config, conversation_id: UUID, source: Path):
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise PigeonError("file_type", "Choose a regular file.")
            data = file.read(MAX_ATTACHMENT + 1)
    except OSError as exc:
        raise PigeonError("file_read", "Could not read the selected file.") from exc
    if not 0 < len(data) <= MAX_ATTACHMENT:
        raise PigeonError("size", "Attachment must be between 1 byte and 1 MiB.")
    query = urlencode({"filename": source.name})
    body, _ = transfer(config, f"/v1/sessions/{conversation_id}/attachments?{query}", data=data)
    result = json.loads(body)
    if result["sha256"] != hashlib.sha256(data).hexdigest():
        raise PigeonError("integrity", "Relay reported a different attachment digest.")
    result["reference"] = f"pigeon-attachment:{result['id']}"
    return result


def download_file(
    config: Config,
    conversation_id: UUID,
    attachment_id: UUID,
    target: Path,
    expected_sha256: str | None = None,
):
    data, headers = transfer(config, f"/v1/sessions/{conversation_id}/attachments/{attachment_id}")
    digest = hashlib.sha256(data).hexdigest()
    if (
        not 0 < len(data) <= MAX_ATTACHMENT
        or digest != headers.get("X-Pigeon-SHA256")
        or (expected_sha256 is not None and digest != expected_sha256)
    ):
        raise PigeonError("integrity", "Attachment failed integrity verification.")
    try:
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            try:
                file.write(data)
                file.flush()
            except OSError:
                target.unlink(missing_ok=True)
                raise
    except FileExistsError:
        raise PigeonError("file_exists", "Choose a destination that does not exist.") from None
    except OSError as exc:
        raise PigeonError("file_write", "Could not save the attachment.") from exc
    return {"path": str(target), "sha256": digest, "bytes": len(data)}
