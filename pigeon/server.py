import asyncio
import sqlite3
from collections import Counter
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import monotonic

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from .client import is_loopback
from .db import Store, token_hash
from .models import (
    ACTIVE,
    MAX_FRAME,
    Cancel,
    Command,
    Finish,
    Grant,
    Inspect,
    ListCalls,
    PigeonError,
    Ping,
    Submit,
    Take,
    command,
)


def create_app(path: Path) -> FastAPI:
    store = Store(path)
    changed = asyncio.Event()
    connections: Counter[str] = Counter()
    buckets: dict[str, tuple[float, float]] = {}

    def notify():
        nonlocal changed
        changed.set()
        changed = asyncio.Event()

    async def sweep():
        while True:
            await asyncio.sleep(1)
            try:
                store.expire()
            except sqlite3.OperationalError:
                continue
            # Also recheck credentials and state changed by local admin commands.
            notify()

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ok", "protocol": 1}

    def rate_limit(agent: str):
        now = monotonic()
        balance, last = buckets.get(agent, (40.0, now))
        balance = min(40.0, balance + (now - last) * 5)
        if balance < 1:
            raise PigeonError("rate_limited", "Slow down and try again.")
        buckets[agent] = (balance - 1, now)

    async def dispatch(agent: str, digest: str, frame: Command):
        if isinstance(frame, (Inspect, Take)):
            until = monotonic() + frame.wait
            while True:
                if store.authenticate(digest) != agent:
                    raise PigeonError("unauthenticated", "Credential is no longer valid.")
                event = changed
                if isinstance(frame, Inspect):
                    result = store.inspect(agent, frame)
                    if result["status"] != frame.after or result["status"] not in ACTIVE:
                        return result
                else:
                    result = store.take(agent)
                    if result:
                        notify()
                        return result
                try:
                    await asyncio.wait_for(event.wait(), max(0, until - monotonic()))
                except TimeoutError:
                    return result
        match frame:
            case Submit():
                result = store.submit(agent, frame)
            case Cancel():
                result = store.inspect(agent, frame)
            case Finish():
                result = store.finish(agent, frame)
            case Grant():
                result = store.grant(agent, frame.sender, frame.op == "allow")
            case ListCalls():
                return store.list_calls(agent)
            case Ping():
                return {"agent": agent, "protocol": 1}
        notify()
        return result

    @app.websocket("/v1/ws")
    async def connect(socket: WebSocket):
        scheme, _, credential = socket.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not credential.isascii() or len(credential) != 43:
            await socket.close(code=1008)
            return
        digest = token_hash(credential)
        agent = store.authenticate(digest)
        if agent is None or connections[agent] >= 16:
            await socket.close(code=1008)
            return
        connections[agent] += 1
        receive = None
        operation = None
        try:
            await socket.accept()
            receive = asyncio.create_task(socket.receive())
            while True:
                incoming = await receive
                if incoming["type"] == "websocket.disconnect":
                    break
                receive = asyncio.create_task(socket.receive())
                raw = incoming.get("text")
                if raw is None or len(raw.encode()) > MAX_FRAME:
                    await socket.close(code=1009)
                    break
                try:
                    if store.authenticate(digest) != agent:
                        raise PigeonError("unauthenticated", "Credential is no longer valid.")
                    rate_limit(agent)
                    frame = command.validate_json(raw)
                    operation = asyncio.create_task(dispatch(agent, digest, frame))
                    await asyncio.wait((operation, receive), return_when=asyncio.FIRST_COMPLETED)
                    if receive.done() and receive.result()["type"] == "websocket.disconnect":
                        break
                    response = {"ok": await operation}
                except ValidationError:
                    response = {"error": "invalid_frame", "message": "Invalid protocol frame."}
                except PigeonError as exc:
                    response = {"error": exc.code, "message": str(exc)}
                except sqlite3.OperationalError:
                    response = {"error": "storage_unavailable", "message": "Relay storage is busy."}
                await socket.send_json(response)
                if response.get("error") == "unauthenticated":
                    await socket.close(code=1008)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            pending = [task for task in (receive, operation) if task is not None]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            connections[agent] -= 1
            if not connections[agent]:
                del connections[agent]

    return app


def serve(
    path: Path,
    host: str,
    port: int,
    cert: Path | None = None,
    key: Path | None = None,
    *,
    behind_tls_proxy: bool = False,
):
    if bool(cert) != bool(key) or (not cert and not behind_tls_proxy and not is_loopback(host)):
        raise PigeonError("tls_required", "Supply a TLS certificate and key for a public bind.")
    uvicorn.run(
        create_app(path),
        host=host,
        port=port,
        ssl_certfile=cert,
        ssl_keyfile=key,
        ws="websockets-sansio",
        ws_max_size=MAX_FRAME,
        ws_per_message_deflate=False,
        timeout_graceful_shutdown=3,
    )
