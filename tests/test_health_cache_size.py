"""/api/health must not walk the whole thumbnail cache on every call, nor fail when a file vanishes.

On a real library the walk took 11 s per call (Settings is the only page that asks), and a thumbnail
deleted during it made the endpoint a 500.
"""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from photointel.api import routes_system


def test_cache_size_is_remembered_between_calls(ctx):
    from photointel.api.app import create_app

    routes_system._cache_bytes_memo.clear()
    client = TestClient(create_app(ctx))
    (ctx.paths.thumbs).mkdir(parents=True, exist_ok=True)
    (ctx.paths.thumbs / "a.jpg").write_bytes(b"x" * 1000)
    first = client.get("/api/health").json()["cache_bytes"]
    (ctx.paths.thumbs / "b.jpg").write_bytes(b"x" * 5000)
    second = client.get("/api/health").json()["cache_bytes"]
    assert first == 1000 and second == 1000, "the second call walked the cache again"
    routes_system._cache_bytes_memo.clear()
    assert client.get("/api/health").json()["cache_bytes"] == 6000


def test_a_file_vanishing_mid_walk_is_not_an_error(tmp_path, monkeypatch):
    (tmp_path / "keep.jpg").write_bytes(b"x" * 10)
    (tmp_path / "gone.jpg").write_bytes(b"x" * 99)
    real = os.scandir

    class Vanishing:
        def __init__(self, entry):
            self._e, self.path, self.name = entry, entry.path, entry.name

        def is_dir(self, **kw):
            return self._e.is_dir(**kw)

        def stat(self, **kw):
            if self.name == "gone.jpg":
                raise FileNotFoundError(self.path)
            return self._e.stat(**kw)

    class Listing:
        def __init__(self, path):
            self._it = real(path)

        def __enter__(self):
            return (Vanishing(e) for e in self._it)

        def __exit__(self, *exc):
            self._it.close()

    monkeypatch.setattr(os, "scandir", Listing)
    assert routes_system._dir_bytes(tmp_path) == 10


def test_a_slow_measurement_does_not_hold_up_health(tmp_path, monkeypatch):
    """Measured in the background: a request waits at most a moment, then gets the last figure."""
    import threading

    routes_system._cache_bytes_memo.clear()
    release = threading.Event()
    monkeypatch.setattr(routes_system, "_dir_bytes", lambda root: (release.wait(10), 42)[1])
    assert routes_system._cache_bytes((tmp_path,), wait=0.1) is None        # not known yet, and no waiting
    release.set()
    for _ in range(100):
        if routes_system._cache_bytes((tmp_path,), wait=0.1) == 42:
            break
    assert routes_system._cache_bytes((tmp_path,), wait=0.1) == 42
