import asyncio
import os
import sqlite3
from collections import Counter
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import monotonic
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import ValidationError

from .client import is_loopback
from .db import open_store, token_hash
from .models import (
    ACTIVE,
    MAX_ATTACHMENT,
    MAX_FRAME,
    RELAY_FEATURES,
    ActivityWatch,
    Cancel,
    Capabilities,
    Close,
    Command,
    Finish,
    Grant,
    History,
    Inspect,
    ListCalls,
    PigeonError,
    Ping,
    Redeem,
    Renew,
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
    changes: dict[str, asyncio.Event] = {}
    connections: Counter[str] = Counter()
    buckets: dict[str, tuple[float, float]] = {}

    def notify(*agents: str):
        for agent in agents or tuple(changes):
            event = changes.pop(agent, None)
            if event is not None:
                event.set()

    def notify_turn(turn):
        request = turn["request"]
        notify(request["sender"], request["recipient"])

    async def sweep():
        while True:
            await asyncio.sleep(1)
            try:
                store.expire()
            except sqlite3.OperationalError:
                continue
            # Also recheck credentials and state changed by local admin commands.
            notify()

    async def prune():
        while True:
            await asyncio.sleep(60)
            with suppress(sqlite3.OperationalError):
                await storage(store.prune_attachments)

    @asynccontextmanager
    async def lifespan(_app):
        if shared:
            yield
            return
        task = asyncio.create_task(sweep())
        cleanup = asyncio.create_task(prune())
        try:
            yield
        finally:
            task.cancel()
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(asyncio.CancelledError):
                await cleanup

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

    async def http_agent(request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.isascii() or len(token) != 43:
            raise HTTPException(401, "Missing relay credential.")
        agent = await storage(store.authenticate, token_hash(token))
        if agent is None:
            raise HTTPException(401, "Invalid relay credential.")
        try:
            rate_limit(agent)
        except PigeonError as exc:
            raise HTTPException(429, str(exc)) from None
        return agent

    @app.post("/v1/sessions/{conversation_id}/attachments", status_code=201)
    async def upload(request: Request, conversation_id: UUID, filename: str):
        agent = await http_agent(request)
        if (
            not 0 < len(filename) <= 120
            or filename in {".", ".."}
            or any(char in "/\\" or not char.isprintable() for char in filename)
        ):
            raise HTTPException(400, "Use a plain filename of at most 120 characters.")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_ATTACHMENT:
                raise HTTPException(413, "Attachment exceeds 1 MiB.")
            data.extend(chunk)
        try:
            return await storage(
                store.upload_attachment, agent, str(conversation_id), filename, bytes(data)
            )
        except PigeonError as exc:
            code = {"not_found": 404, "unsupported": 409, "limit": 429}.get(exc.code, 400)
            raise HTTPException(code, str(exc)) from None

    @app.get("/v1/sessions/{conversation_id}/attachments/{attachment_id}")
    async def download(request: Request, conversation_id: UUID, attachment_id: UUID):
        agent = await http_agent(request)
        try:
            row = await storage(
                store.download_attachment, agent, str(conversation_id), str(attachment_id)
            )
        except PigeonError:
            raise HTTPException(404, "Attachment not found.") from None
        return Response(
            bytes(row["data"]),
            media_type="application/octet-stream",
            headers={
                "X-Pigeon-SHA256": row["digest"],
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def dispatch(agent: str, digest: str, frame: Command):
        if isinstance(frame, (Inspect, Take, Watch, ActivityWatch)):
            until = monotonic() + frame.wait
            while True:
                if not isinstance(frame, ActivityWatch) and (
                    await storage(store.authenticate, digest) != agent
                ):
                    raise PigeonError("unauthenticated", "Credential is no longer valid.")
                event = changes.setdefault(agent, asyncio.Event())
                if isinstance(frame, Inspect):
                    if shared:
                        await storage(store.expire)
                    result = await storage(store.inspect, agent, frame)
                    if result["status"] != frame.after or result["status"] not in ACTIVE:
                        return result
                elif isinstance(frame, ActivityWatch):
                    result = await storage(store.watch_activity, agent, frame, digest)
                    if result:
                        return result
                elif isinstance(frame, Watch):
                    result = await storage(store.next_message, agent)
                    if result:
                        return result
                else:
                    result = await storage(store.take, agent, frame.claim_id)
                    if result:
                        notify_turn(result)
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
            case Finish():
                result = await storage(store.finish, agent, frame)
            case Renew():
                result = await storage(store.renew, agent, frame)
            case History():
                return await storage(store.history, agent, frame)
            case Grant():
                result = await storage(store.grant, agent, frame.sender, frame.op == "allow")
            case ListCalls():
                if shared:
                    await storage(store.expire)
                return await storage(store.list_calls, agent)
            case Capabilities():
                conversation_id = str(frame.conversation_id) if frame.conversation_id else None
                return await storage(store.capabilities, agent, conversation_id, frame.features)
            case Share():
                return await storage(
                    store.share, agent, frame.session_id, frame.session_token, frame.resume
                )
            case Redeem():
                return await storage(store.redeem, agent, frame.key, frame.peer_session)
            case Ping():
                return {"agent": agent, "protocol": 1, "features": RELAY_FEATURES}
        if isinstance(frame, (Submit, Cancel, Finish, Renew)):
            notify_turn(result)
        else:
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
                    frame = command.validate_json(raw)
                    if not isinstance(frame, (Inspect, Take, Watch, ActivityWatch)) and (
                        await storage(store.authenticate, digest) != agent
                    ):
                        raise PigeonError("unauthenticated", "Credential is no longer valid.")
                    rate_limit(agent)
                    operation = asyncio.create_task(dispatch(agent, digest, frame))
                    await asyncio.wait((operation, receive), return_when=asyncio.FIRST_COMPLETED)
                    if receive.done() and receive.result()["type"] == "websocket.disconnect":
                        break
                    response = {"ok": await operation}
                except ValidationError:
                    response = {"error": "invalid_frame", "message": "Invalid protocol frame."}
                except PigeonError as exc:
                    response = {"error": exc.code, "message": str(exc)}
                    if exc.data is not None:
                        response["data"] = exc.data
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
                changes.pop(agent, None)

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
