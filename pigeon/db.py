import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .models import Cancel, Finish, Inspect, PigeonError, Submit, address

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    address TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS grants (
    recipient TEXT REFERENCES agents NOT NULL,
    sender TEXT REFERENCES agents NOT NULL,
    PRIMARY KEY (recipient, sender)
);
CREATE TABLE IF NOT EXISTS session_shares (
    recipient TEXT PRIMARY KEY REFERENCES agents,
    owner TEXT NOT NULL REFERENCES agents,
    key_hash TEXT NOT NULL UNIQUE,
    expires_at REAL NOT NULL,
    reviewer TEXT REFERENCES agents
);
CREATE INDEX IF NOT EXISTS shares_by_owner ON session_shares(owner, expires_at);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    sender TEXT REFERENCES agents NOT NULL,
    recipient TEXT REFERENCES agents NOT NULL,
    created_at REAL NOT NULL,
    context TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations NOT NULL,
    request TEXT NOT NULL,
    submitted_context TEXT,
    timeout INTEGER NOT NULL,
    created_at REAL NOT NULL,
    deadline REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    response_id TEXT,
    response TEXT,
    responded_at REAL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS conversation_turns ON turns(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS active_turns ON turns(status, deadline)
    WHERE status IN ('queued', 'running');
"""

TURN = """
SELECT t.*, c.sender, c.recipient, c.context, c.created_at AS conversation_created_at,
       EXISTS(SELECT 1 FROM turns earlier
              WHERE earlier.conversation_id=t.conversation_id AND earlier.rowid<t.rowid)
          AS follow_up
FROM turns t JOIN conversations c ON c.id = t.conversation_id
"""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def render_turn(row: sqlite3.Row) -> dict:
    request = {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "content": row["request"],
        "created_at": row["created_at"],
        "type": "request",
    }
    response = None
    if row["response_id"]:
        response = {
            "id": row["response_id"],
            "conversation_id": row["conversation_id"],
            "sender": row["recipient"],
            "recipient": row["sender"],
            "content": row["response"],
            "created_at": row["responded_at"],
            "type": "response" if row["status"] == "completed" else "error",
        }
    return {
        "request": request,
        "response": response,
        "conversation_created_at": row["conversation_created_at"],
        "context": json.loads(row["context"]) if row["context"] else None,
        "deadline": row["deadline"],
        "status": row["status"],
        "detail": row["detail"],
        "follow_up": bool(row["follow_up"]),
    }


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        if path.stat().st_mode & 0o077:
            raise PigeonError("database_permissions", f"Run chmod 600 on {path}.")
        self.path = path
        with self.transaction(write=False) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, write: bool = True):
        db = sqlite3.connect(self.path, timeout=1, autocommit=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        finally:
            db.close()  # Rolls back on exceptions.

    def enroll(self, name: str, *, rotate: bool = False) -> str:
        name = address.validate_python(name)
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            if rotate:
                count = db.execute(
                    "UPDATE agents SET token_hash=? WHERE address=?", (token_hash(token), name)
                ).rowcount
                if not count:
                    raise PigeonError("unknown_agent", "Agent is not registered.")
            else:
                if not db.execute(
                    "INSERT OR IGNORE INTO agents VALUES (?, ?)", (name, token_hash(token))
                ).rowcount:
                    raise PigeonError("registered", "Agent already exists. Use --rotate.")
        return token

    def revoke(self, name: str):
        with self.transaction() as db:
            if not db.execute(
                "UPDATE agents SET token_hash=NULL WHERE address=?", (name,)
            ).rowcount:
                raise PigeonError("unknown_agent", "Agent is not registered.")
            db.execute(
                """UPDATE turns SET status='cancelled', detail='Agent credential revoked.'
                   WHERE status IN ('queued', 'running') AND conversation_id IN
                   (SELECT id FROM conversations WHERE sender=? OR recipient=?)""",
                (name, name),
            )

    def authenticate(self, digest: str) -> str | None:
        with self.transaction(write=False) as db:
            row = db.execute("SELECT address FROM agents WHERE token_hash=?", (digest,)).fetchone()
        return row["address"] if row else None

    def grant(self, recipient: str, sender: str, allow: bool):
        with self.transaction() as db:
            if allow:
                if (
                    sender == recipient
                    or not db.execute("SELECT 1 FROM agents WHERE address=?", (sender,)).fetchone()
                ):
                    raise PigeonError("invalid_peer", "Choose another registered agent.")
                db.execute("INSERT OR IGNORE INTO grants VALUES (?, ?)", (recipient, sender))
            else:
                db.execute("DELETE FROM grants WHERE recipient=? AND sender=?", (recipient, sender))
                db.execute(
                    """UPDATE turns SET status='cancelled', detail='Peer authorization revoked.'
                       WHERE status IN ('queued', 'running') AND conversation_id IN
                       (SELECT id FROM conversations WHERE sender=? AND recipient=?)""",
                    (sender, recipient),
                )

    def share(self, owner: str) -> dict:
        now = time.time()
        name = f"{owner.split('/')[0]}/pr-{secrets.token_hex(8)}"
        key = "pg_" + secrets.token_urlsafe(16)
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM session_shares WHERE recipient=?", (owner,)).fetchone():
                raise PigeonError("invalid_share", "Share from your main Pigeon identity.")
            count = db.execute(
                "SELECT count(*) AS total FROM session_shares WHERE owner=? AND expires_at>?",
                (owner, now),
            ).fetchone()["total"]
            if count >= 10:
                raise PigeonError("share_limit", "You already have ten active session keys.")
            db.execute("INSERT INTO agents VALUES (?, ?)", (name, token_hash(token)))
            db.execute(
                "INSERT INTO session_shares VALUES (?, ?, ?, ?, NULL)",
                (name, owner, token_hash(key), now + 3600),
            )
        return {"address": name, "key": key, "token": token}

    def redeem(self, reviewer: str, key: str) -> dict:
        with self.transaction() as db:
            share = db.execute(
                "SELECT * FROM session_shares WHERE key_hash=? AND expires_at>?",
                (token_hash(key), time.time()),
            ).fetchone()
            if not share or share["reviewer"] not in (None, reviewer):
                raise PigeonError("invalid_key", "This session key is invalid, expired, or in use.")
            db.execute(
                "UPDATE session_shares SET reviewer=? WHERE recipient=?",
                (reviewer, share["recipient"]),
            )
            db.execute("INSERT OR IGNORE INTO grants VALUES (?, ?)", (share["recipient"], reviewer))
        return {"address": share["recipient"]}

    def expire(self) -> bool:
        with self.transaction() as db:
            result = db.execute(
                """UPDATE turns SET detail=CASE status
                       WHEN 'running' THEN 'Deadline elapsed. Execution outcome is unknown.'
                       ELSE 'Deadline elapsed before a daemon accepted the request.' END,
                       status='timed_out'
                   WHERE status IN ('queued', 'running') AND deadline <= ?""",
                (time.time(),),
            )
            return bool(result.rowcount)

    def submit(self, sender: str, request: Submit) -> dict:
        conversation_id = str(request.conversation_id)
        message_id = str(request.message_id)
        context = request.context.model_dump_json() if request.context else None
        now = time.time()
        with self.transaction() as db:
            existing = db.execute(TURN + " WHERE t.id=?", (message_id,)).fetchone()
            if existing:
                submitted = (
                    sender,
                    request.to,
                    conversation_id,
                    request.message,
                    context,
                    request.timeout,
                )
                stored = tuple(
                    existing[key]
                    for key in (
                        "sender",
                        "recipient",
                        "conversation_id",
                        "request",
                        "submitted_context",
                        "timeout",
                    )
                )
                if submitted != stored:
                    raise PigeonError("conflict", "Message ID is already in use.")
                return render_turn(existing)
            if not db.execute(
                """SELECT 1 FROM grants g JOIN agents a ON a.address=g.recipient
                   WHERE g.sender=? AND g.recipient=? AND a.token_hash IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM session_shares s WHERE s.recipient=g.recipient
                       AND (s.expires_at<=? OR s.reviewer IS NOT g.sender)
                   )""",
                (sender, request.to, now),
            ).fetchone():
                raise PigeonError("forbidden", "Recipient is unavailable or has not allowed you.")
            conversation = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if conversation:
                if (conversation["sender"], conversation["recipient"]) != (sender, request.to):
                    raise PigeonError("not_found", "Conversation not found.")
                if context is not None and context != conversation["context"]:
                    raise PigeonError("conflict", "Conversation context is fixed.")
                previous = db.execute(
                    """SELECT status FROM turns WHERE conversation_id=?
                       ORDER BY rowid DESC LIMIT 1""",
                    (conversation_id,),
                ).fetchone()
                if previous["status"] != "completed":
                    raise PigeonError(
                        "conflict", "Follow-ups require a completed turn. Check its status."
                    )
            else:
                db.execute(
                    "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                    (conversation_id, sender, request.to, now, context),
                )
            pending = db.execute(
                """SELECT 1 FROM turns t JOIN conversations c ON c.id=t.conversation_id
                   WHERE t.status IN ('queued', 'running') AND c.recipient=? LIMIT 64""",
                (request.to,),
            ).fetchall()
            if len(pending) >= 64:
                raise PigeonError("busy", "Recipient's queue is full. Try again later.")
            db.execute(
                """INSERT INTO turns
                   (id, conversation_id, request, submitted_context, timeout, created_at, deadline)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    conversation_id,
                    request.message,
                    context,
                    request.timeout,
                    now,
                    now + request.timeout,
                ),
            )
            return render_turn(db.execute(TURN + " WHERE t.id=?", (message_id,)).fetchone())

    def inspect(self, agent: str, request: Inspect | Cancel) -> dict:
        with self.transaction(write=isinstance(request, Cancel)) as db:
            query = TURN + " WHERE c.id=? AND (c.sender=? OR c.recipient=?)"
            params = [str(request.conversation_id), agent, agent]
            if request.message_id:
                query += " AND t.id=?"
                params.append(str(request.message_id))
            query += " ORDER BY t.rowid DESC LIMIT 1"
            row = db.execute(query, params).fetchone()
            if not row:
                raise PigeonError("not_found", "Conversation or message not found.")
            if isinstance(request, Cancel):
                if row["sender"] != agent:
                    raise PigeonError("forbidden", "Only the caller can cancel a request.")
                db.execute(
                    """UPDATE turns SET status='cancelled', detail='Cancelled by caller.'
                       WHERE id=? AND status IN ('queued', 'running')""",
                    (row["id"],),
                )
                row = db.execute(query, params).fetchone()
            return render_turn(row)

    def list_calls(self, agent: str) -> list[dict]:
        with self.transaction(write=False) as db:
            rows = db.execute(
                """SELECT c.id AS conversation_id, c.sender, c.recipient, t.id AS message_id,
                          t.created_at, t.status
                   FROM conversations c JOIN turns t ON t.rowid=(
                       SELECT rowid FROM turns WHERE conversation_id=c.id
                       ORDER BY rowid DESC LIMIT 1
                   )
                   WHERE c.sender=? OR c.recipient=?
                   ORDER BY t.created_at DESC LIMIT 50""",
                (agent, agent),
            ).fetchall()
        return [dict(row) for row in rows]

    def take(self, recipient: str) -> dict | None:
        with self.transaction() as db:
            row = db.execute(
                TURN
                + """ WHERE c.recipient=? AND t.status='queued' AND t.deadline>?
                    ORDER BY t.rowid LIMIT 1""",
                (recipient, time.time()),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE turns SET status='running' WHERE id=?", (row["id"],))
            return render_turn(db.execute(TURN + " WHERE t.id=?", (row["id"],)).fetchone())

    def finish(self, recipient: str, result: Finish) -> dict:
        message_id = str(result.message_id)
        with self.transaction() as db:
            query = TURN + " WHERE t.id=? AND c.recipient=?"
            params = (message_id, recipient)
            row = db.execute(query, params).fetchone()
            if not row:
                raise PigeonError("not_found", "Request not found.")
            if row["status"] == "queued":
                raise PigeonError("conflict", "Request must be claimed before completion.")
            if row["status"] == "running" and row["deadline"] <= time.time():
                db.execute(
                    """UPDATE turns SET status='timed_out',
                       detail='Deadline elapsed. Execution outcome is unknown.' WHERE id=?""",
                    (message_id,),
                )
                row = db.execute(query, params).fetchone()
            if row["status"] == "running":
                db.execute(
                    """UPDATE turns SET status=?, response_id=?, response=?, responded_at=?
                       WHERE id=?""",
                    (result.status, str(uuid4()), result.content, time.time(), message_id),
                )
                row = db.execute(query, params).fetchone()
            return render_turn(row)


class _RemoteCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.columns = [column[0] for column in cursor.description or ()]

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(zip(self.columns, row, strict=True)) if row is not None else None

    def fetchall(self):
        return [dict(zip(self.columns, row, strict=True)) for row in self.cursor.fetchall()]


class _RemoteConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, args=()):
        return _RemoteCursor(self.connection.execute(sql, tuple(args)))


class RemoteStore(Store):
    def __init__(self, url: str, token: str):
        if not url.startswith(("libsql://", "https://")) or not token:
            raise PigeonError("database_config", "Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN.")
        self.url = url
        self.token = token
        with self.transaction() as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    @contextmanager
    def transaction(self, *, write: bool = True):
        import libsql

        connection = None
        try:
            connection = libsql.connect(
                self.url, auth_token=self.token, isolation_level=None, timeout=5
            )
            db = _RemoteConnection(connection)
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except (ValueError, RuntimeError) as exc:
            raise sqlite3.OperationalError("Shared database request failed.") from exc
        finally:
            if connection is not None:
                connection.close()


def open_store(path: Path | None) -> Store:
    if path is not None:
        return Store(path)
    return RemoteStore(
        os.environ.get("TURSO_DATABASE_URL", ""), os.environ.get("TURSO_AUTH_TOKEN", "")
    )
