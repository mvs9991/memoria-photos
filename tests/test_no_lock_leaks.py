"""No web request may leave the database write-locked once it has answered.

The background indexer is a separate process that writes to the same SQLite file. A request
handler that writes and forgets to commit keeps a transaction open on its (reused) connection, so
the indexer's own writes then wait out the 60 s busy timeout. A job's post-processing did exactly
that to itself (see test_post_transactions), turning 0.2 s of work into minutes.

The check is behavioural rather than a read of the code: after each request, a second connection
must be able to take the write lock at once.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from photointel import db as db_mod
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from tests.conftest import make_image
from tests.test_api_fuzz import fill, get_endpoints
from tests.test_api_fuzz_writes import BODIES, SKIP, mutating_endpoints
from tests.test_api_fuzz_writes import fill as fill_write


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    return create_app(ctx)


@pytest.fixture
def opened(monkeypatch):
    """Every connection the app opens, so a leak can be undone and reported exactly once."""
    made: list[sqlite3.Connection] = []
    real = db_mod.connect

    def recording(*a, **kw):
        c = real(*a, **kw)
        made.append(c)
        return c

    monkeypatch.setattr(db_mod, "connect", recording)
    return made


def release(made: list[sqlite3.Connection]) -> None:
    for c in made:
        try:
            c.rollback()
        except sqlite3.Error:
            pass


@pytest.fixture
def library_ready(ctx, tmp_path):
    root = tmp_path / "lib"
    for i in range(6):
        make_image(root / f"p{i}.jpg", colour=(40 + 30 * i, 90, 160),
                   taken=datetime(2024, 3, 9, 11, i), gps=(17.385, 78.4867), noise=16)
    Indexer(ctx, workers=2).run(roots=[str(root)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    conn.close()
    return root


def write_lock_is_free(ctx) -> bool:
    c = sqlite3.connect(str(ctx.paths.db), timeout=0.25)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        c.close()


def test_the_database_starts_out_unlocked(ctx, app, library_ready):
    TestClient(app, raise_server_exceptions=False).get("/api/stats")
    assert write_lock_is_free(ctx)


def test_no_get_endpoint_leaves_the_database_write_locked(ctx, app, library_ready, opened):
    client = TestClient(app, raise_server_exceptions=False)
    conn = ctx.connect()
    photo_id = int(conn.execute("SELECT id FROM photos LIMIT 1").fetchone()[0])
    conn.close()

    endpoints = [fill(p, photo_id) for p, _ in get_endpoints(app)]
    endpoints = [p for p in endpoints if p]
    assert len(endpoints) > 40

    leaks = []
    for path in endpoints:
        client.get(path)
        if not write_lock_is_free(ctx):
            leaks.append(path)
            release(opened)          # so this leak is reported once, not blamed on every endpoint after it
    assert not leaks, "these endpoints left the database write-locked after answering:\n" + "\n".join(leaks)


def test_no_mutating_endpoint_leaves_it_locked_even_for_bad_input(ctx, app, library_ready, opened):
    client = TestClient(app, raise_server_exceptions=False)
    conn = ctx.connect()
    photo_id = int(conn.execute("SELECT id FROM photos LIMIT 1").fetchone()[0])
    conn.close()

    leaks = []
    for method, path in mutating_endpoints(app):
        filled = fill_write(path, photo_id)
        if not filled or path in SKIP:
            continue
        for body in BODIES:
            client.request(method.upper(), filled, json=body)
            if not write_lock_is_free(ctx):
                leaks.append(f"{method.upper()} {filled} body={str(body)[:50]}")
                release(opened)
                break
    assert not leaks, "these endpoints left the database write-locked:\n" + "\n".join(leaks[:30])


def test_the_audit_can_actually_see_a_leak(ctx, app, library_ready, opened):
    """A check that cannot fail proves nothing: plant an endpoint that writes and never commits."""
    from photointel.api.deps import get_state

    def leaky():
        get_state().conn().execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('leak', '1')")
        return {"ok": True}

    app.add_api_route("/api/_leaky", leaky, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())      # ahead of the SPA catch-all
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/_leaky").json() == {"ok": True}
    assert not write_lock_is_free(ctx), "the planted leak went unnoticed, so this audit would miss a real one"
    release(opened)
    assert write_lock_is_free(ctx)
