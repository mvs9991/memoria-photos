"""Whole-library answers are kept until the database changes, and never a moment longer."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel.api import cache
from photointel.pipeline.indexer import Indexer


@pytest.fixture
def client(ctx, library):
    from photointel.api.app import create_app

    Indexer(ctx, workers=2).run(roots=[str(library)])
    cache.clear()
    return TestClient(create_app(ctx))


def test_a_repeat_is_kept_and_any_write_ends_it(ctx, client, monkeypatch):
    from photointel.api import routes_library

    calls = []
    real = routes_library.favorites.count
    monkeypatch.setattr(routes_library.favorites, "count", lambda *a: (calls.append(1), real(*a))[1])
    first = client.get("/api/stats").json()
    assert client.get("/api/stats").json() == first and len(calls) == 1      # the second came from the cache

    pid = ctx.connect().execute("SELECT id FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 "
                                "LIMIT 1").fetchone()[0]
    client.post("/api/photos/hide", json={"photo_ids": [pid]})                 # a write through the app
    assert client.get("/api/stats").json()["photos"] == first["photos"] - 1 and len(calls) == 2

    other = ctx.connect()                                                     # an index job is another process
    other.execute("UPDATE photos SET hidden = 1 WHERE id = (SELECT MIN(id) FROM photos WHERE status = 'ok' "
                  "AND hidden = 0 AND live_component = 0)")
    other.commit()
    assert client.get("/api/stats").json()["photos"] == first["photos"] - 2 and len(calls) == 3


def test_answers_differ_by_their_arguments(client):
    everyone = client.get("/api/timeline").json()
    assert client.get("/api/timeline", params={"person": 999999}).json() != everyone
    assert client.get("/api/timeline").json() == everyone
