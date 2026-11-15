"""Count store connections and measure submit-to-claim latency with idle WebSockets."""

import argparse
import asyncio
import secrets
import socket
from contextlib import contextmanager
from pathlib import Path
from statistics import median, quantiles
from tempfile import TemporaryDirectory
from threading import Lock
from time import perf_counter
from unittest.mock import patch

import uvicorn

from pigeon.client import open_client
from pigeon.db import Store, token_hash
from pigeon.models import ActivityWatch, Finish, Take
from pigeon.server import create_app


class MeteredStore(Store):
    def __init__(self, path):
        self.connections_opened = 0
        self.counter_lock = Lock()
        super().__init__(path)

    @contextmanager
    def transaction(self, *, write=True):
        with self.counter_lock:
            self.connections_opened += 1
        with super().transaction(write=write) as db:
            yield db


async def benchmark(store, count, seconds):
    tokens = [secrets.token_urlsafe(32) for _ in range(count)]
    with store.transaction() as db:
        for i, token in enumerate(tokens):
            db.execute("INSERT INTO agents VALUES (?, ?)", (f"@load{i}/agent", token_hash(token)))
    sender = store.enroll("@bench/sender")
    receiver = store.enroll("@bench/receiver")
    store.grant("@bench/receiver", "@bench/sender", True)
    with patch("pigeon.server.open_store", return_value=store):
        app = create_app(None)
    # Use the hosted polling and worker-thread path with a disposable SQLite file.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2048)
        url = f"ws://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="websockets-sansio"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        ready = asyncio.Queue()
        begin = asyncio.Event()

        async def idle(token):
            async with open_client(url, token) as client:
                ready.put_nowait(None)
                await begin.wait()
                while True:
                    await client.call(ActivityWatch(wait=5))

        tasks = []
        try:
            # Stagger handshakes so startup noise does not dominate the idle window.
            for offset in range(0, count, 200):
                batch = tokens[offset : offset + 200]
                tasks.extend(asyncio.create_task(idle(token)) for token in batch)
                async with asyncio.timeout(20):
                    for _ in batch:
                        await ready.get()
            baseline = store.connections_opened
            start = perf_counter()
            begin.set()
            await asyncio.sleep(seconds)
            idle_seconds = perf_counter() - start
            opened = store.connections_opened - baseline
            live = sum(not task.done() for task in tasks)
            print(
                f"{count} clients: {live} still connected, "
                f"{opened} idle store connections in {idle_seconds:.2f}s "
                f"({opened / idle_seconds / count:.2f}/listener/s)",
                flush=True,
            )
            samples = []
            async with open_client(url, sender) as source, open_client(url, receiver) as target:
                for _ in range(12):
                    start = perf_counter()
                    turn = await source.ask("@bench/receiver", "benchmark", wait=0)
                    claimed = await target.call(Take(wait=0))
                    assert claimed["request"]["id"] == str(turn.request.id)
                    samples.append(1000 * (perf_counter() - start))
                    await target.call(
                        Finish(message_id=turn.request.id, status="completed", content="done")
                    )
            p95 = quantiles(samples, n=100, method="inclusive")[94]
            print(
                f"{count} clients: submit-to-claim p50/p95 {median(samples):.1f}/{p95:.1f} ms",
                flush=True,
            )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            server.should_exit = True
            await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clients", type=int, nargs="+")
    parser.add_argument("--seconds", type=float, default=8)
    args = parser.parse_args()
    for clients in args.clients:
        with TemporaryDirectory() as folder:
            asyncio.run(
                benchmark(MeteredStore(Path(folder) / "bench.sqlite3"), clients, args.seconds)
            )
