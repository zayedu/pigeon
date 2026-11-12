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
