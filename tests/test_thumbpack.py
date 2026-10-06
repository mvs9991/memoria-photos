"""The small thumbnails packed into one file in grid order: same bytes, resumable build, cache-clear safe."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel.engine import thumbpack, thumbs
from photointel.pipeline.indexer import Indexer


@pytest.fixture
def indexed(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    yield ctx
    thumbpack.close()


def _grid_order(conn):
    return [r[0] for r in conn.execute(
        "SELECT id, sha256 FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 "
        "AND sha256 IS NOT NULL ORDER BY taken_ts DESC, id DESC")]


def test_the_pack_holds_every_small_thumbnail_in_grid_order_with_the_same_bytes(indexed):
    ctx = indexed
    conn = ctx.connect()
    root = ctx.paths.thumbs
    assert thumbpack.needs_build(conn, root)
    assert thumbpack.build(conn, root, may_run=lambda: True) == "built"
    assert not thumbpack.needs_build(conn, root)
    shas = thumbpack._visible_shas(conn)
    packed = [r[0] for r in thumbpack._conn(root)[0].execute("SELECT sha FROM pack ORDER BY pos")]
    assert packed == [s for s in shas if thumbs.small_path(root, s).exists()] and packed
    for sha in packed[:5]:
        assert thumbpack.lookup(root, sha) == thumbs.small_path(root, sha).read_bytes()


def test_an_interrupted_build_resumes_and_serving_falls_back_meanwhile(indexed):
    ctx = indexed
    conn = ctx.connect()
    root = ctx.paths.thumbs
    budget = iter([True] * 3 + [False] * 10)
    assert thumbpack.build(conn, root, may_run=lambda: next(budget, False)) == "paused"
    assert thumbpack.lookup(root, thumbpack._visible_shas(conn)[0]) is None        # no pack yet: files serve
    assert thumbpack.build(conn, root, may_run=lambda: True) == "built"
    assert not (root / thumbpack.STAGING).exists()


def test_the_api_serves_the_packed_bytes_and_survives_clearing_the_cache(indexed):
    from photointel.api.app import create_app

    ctx = indexed
    conn = ctx.connect()
    thumbpack.build(conn, ctx.paths.thumbs, may_run=lambda: True)
    client = TestClient(create_app(ctx))
    pid, sha = conn.execute("SELECT id, sha256 FROM photos WHERE status = 'ok' AND hidden = 0 AND "
                            "live_component = 0 AND rotation = 0 LIMIT 1").fetchone()
    r = client.get(f"/api/thumb/{pid}", params={"s": "sm"})
    assert r.status_code == 200 and r.content == thumbpack.lookup(ctx.paths.thumbs, sha)
    assert client.post("/api/cache/clear", params={"kind": "thumbs"}).status_code == 200
    assert not thumbpack._packs(ctx.paths.thumbs)                                  # the pack went too
    assert client.get(f"/api/thumb/{pid}", params={"s": "sm"}).status_code == 200  # and it still serves


def test_a_new_photo_not_yet_packed_is_served_from_its_file(indexed):
    ctx = indexed
    conn = ctx.connect()
    root = ctx.paths.thumbs
    thumbpack.build(conn, root, may_run=lambda: True)
    sha = thumbpack._visible_shas(conn)[0]
    thumbpack.close()
    pc = __import__("sqlite3").connect(thumbpack._packs(root)[0])
    pc.execute("DELETE FROM pack WHERE sha = ?", (sha,))      # as if it arrived after the build
    pc.commit()
    pc.close()
    assert thumbpack.lookup(root, sha) is None
    assert thumbpack.needs_build(conn, root, now=__import__("time").time() + 86400 * 2)
