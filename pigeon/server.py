import asyncio
import os
import sqlite3
from collections import Counter
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import monotonic

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from .client import is_loopback
from .db import open_store, token_hash
from .models import (
    ACTIVE,
    MAX_FRAME,
    Cancel,
    Close,
    Command,
    Finish,
    Grant,
    Inspect,
    ListCalls,
    PigeonError,
    Ping,
    Recover,
    Redeem,
    Share,
    Signup,
    Submit,
    Take,
    Watch,
    command,
)


def create_app(path: Path | None) -> FastAPI:
    store = open_store(path)
    shared = path is None
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
        if shared:
            yield
            return
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

    @app.post("/v1/signup", status_code=201)
    async def signup(request: Request, form: Signup):
        source = request.client.host if request.client else "unknown"
        if os.environ.get("VERCEL") == "1":
            source = request.headers.get("x-vercel-forwarded-for", source)
        try:
            return await storage(store.signup, token_hash(source), form.username)
        except PigeonError as exc:
            raise HTTPException(
                status_code=409 if exc.code == "username_taken" else 429, detail=str(exc)
            ) from None
        except sqlite3.OperationalError:
            raise HTTPException(status_code=503, detail="Relay storage is busy.") from None

    def rate_limit(agent: str):
        now = monotonic()
        balance, last = buckets.get(agent, (40.0, now))
        balance = min(40.0, balance + (now - last) * 5)
        if balance < 1:
            raise PigeonError("rate_limited", "Slow down and try again.")
        buckets[agent] = (balance - 1, now)

    async def storage(operation, *args):
        if shared:
            return await asyncio.to_thread(operation, *args)
        return operation(*args)

    async def dispatch(agent: str, digest: str, frame: Command):
        if isinstance(frame, (Inspect, Take, Watch)):
            until = monotonic() + frame.wait
            while True:
                if await storage(store.authenticate, digest) != agent:
                    raise PigeonError("unauthenticated", "Credential is no longer valid.")
                event = changed
                if isinstance(frame, Inspect):
                    if shared:
                        await storage(store.expire)
                    result = await storage(store.inspect, agent, frame)
                    if result["status"] != frame.after or result["status"] not in ACTIVE:
                        return result
                else:
                    operation = store.next_message if isinstance(frame, Watch) else store.take
                    result = await storage(operation, agent)
                    if result:
                        if isinstance(frame, Take):
                            notify()
                        return result
                remaining = until - monotonic()
                if remaining <= 0:
                    return result
                try:
                    await asyncio.wait_for(event.wait(), min(1, remaining) if shared else remaining)
                except TimeoutError:
                    if monotonic() >= until:
                        return result
        match frame:
            case Submit():
                result = await storage(store.submit, agent, frame)
            case Cancel():
                result = await storage(store.inspect, agent, frame)
            case Close():
                result = await storage(store.close, agent, str(frame.conversation_id))
            case Recover():
                message_id = None if frame.message_id is None else str(frame.message_id)
                return await storage(store.recover, agent, message_id)
            case Finish():
                result = await storage(store.finish, agent, frame)
            case Grant():
                result = await storage(store.grant, agent, frame.sender, frame.op == "allow")
            case ListCalls():
                if shared:
                    await storage(store.expire)
                return await storage(store.list_calls, agent)
            case Share():
                return await storage(
                    store.share, agent, frame.session_id, frame.session_token, frame.resume
                )
            case Redeem():
                return await storage(store.redeem, agent, frame.key)
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
        agent = await storage(store.authenticate, digest)
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
                    if await storage(store.authenticate, digest) != agent:
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
