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
    real = os.stat

    def flaky(path, *a, **kw):
        if str(path).endswith("gone.jpg"):
            raise FileNotFoundError(path)
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "stat", flaky)
    assert routes_system._dir_bytes(tmp_path) == 10
