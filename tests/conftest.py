import socket
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from pigeon.db import Store

ALICE = "@alice/codex"
BOB = "@bob/claude"


@dataclass
class Relay:
    store: Store
    url: str
    alice: str
    bob: str


@pytest.fixture
def identities(tmp_path):
    store = Store(tmp_path / "relay.sqlite3")
    return store, store.enroll(ALICE), store.enroll(BOB)


@contextmanager
def relay_process(store, port=None):
    if port is None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from pigeon.server import serve;"
            "serve(Path(sys.argv[1]), '127.0.0.1', int(sys.argv[2]))",
            str(store.path),
            str(port),
        ]
    )
    try:
        until = monotonic() + 5
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2) as response:
                    assert response.status == 200
                    break
            except (URLError, TimeoutError):
                if proc.poll() is not None or monotonic() >= until:
                    pytest.fail("Relay did not become healthy.")
                sleep(0.02)
        yield f"ws://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def relay(identities):
    store, alice, bob = identities
    with relay_process(store) as url:
        yield Relay(store, url, alice, bob)


class StorageFaults:
    """Make chosen relay Store methods raise transient storage errors."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.calls: dict[str, int] = {}
        self.pending: dict[str, list[bool]] = {}

    def fail(self, method: str, times: int = 1, *, after_commit: bool = False):
        """Fail the next `times` calls. after_commit runs the call first, like a lost reply."""
        import sqlite3

        from pigeon.db import Store

        self.pending[method] = [after_commit] * times
        if method in self.calls:
            return
        self.calls[method] = 0
        original = getattr(Store, method)

        def faulty(store, *args, **kwargs):
            self.calls[method] += 1
            queue = self.pending.get(method)
            if not queue:
                return original(store, *args, **kwargs)
            if queue.pop(0):
                original(store, *args, **kwargs)
            raise sqlite3.OperationalError("injected storage fault")

        self.monkeypatch.setattr(Store, method, faulty)


@pytest.fixture
def faulty_relay(identities, monkeypatch):
    """A relay served in this process, so tests can inject storage faults into it."""
    import threading

    import uvicorn

    from pigeon.server import create_app

    store, alice, bob = identities
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(store.path),
            host="127.0.0.1",
            port=port,
            ws="websockets-sansio",
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    until = monotonic() + 5
    while not server.started:
        if not thread.is_alive() or monotonic() >= until:
            pytest.fail("In-process relay did not start.")
        sleep(0.02)
    try:
        yield Relay(store, f"ws://127.0.0.1:{port}", alice, bob), StorageFaults(monkeypatch)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
