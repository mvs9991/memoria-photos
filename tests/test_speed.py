"""Latency work: compressed text responses, long-cached build files, and merge suggestions that do not
recompare every face on each visit to People. Each one must not change what a response contains."""
from __future__ import annotations

import gzip
from datetime import datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient

from photointel.engine import people as people_mod
from photointel.pipeline.indexer import Indexer
from tests.conftest import make_image


@pytest.fixture
def client(ctx, tmp_path):
    from photointel.api.app import create_app

    root = tmp_path / "lib"
    for i in range(30):
        make_image(root / f"p{i}.jpg", colour=(40 + 5 * i, 90, 160), taken=datetime(2024, 3, 9, 11, i), noise=16)
    Indexer(ctx, workers=2, enable_faces=False, enable_semantic=False).run(roots=[str(root)])
    return TestClient(create_app(ctx))


def test_json_is_compressed_for_clients_that_accept_it_and_means_the_same(client):
    plain = client.get("/api/photos/index", headers={"Accept-Encoding": "identity"})
    zipped = client.get("/api/photos/index", headers={"Accept-Encoding": "gzip"})
    assert plain.headers.get("content-encoding") is None
    assert zipped.headers.get("content-encoding") == "gzip"
    assert zipped.json() == plain.json()                       # httpx decodes it: same content


def test_photos_and_downloads_are_never_recompressed(client):
    pid = client.get("/api/photos/index").json()["ids"][0]
    thumb = client.get(f"/api/thumb/{pid}?size=m", headers={"Accept-Encoding": "gzip"})
    assert thumb.status_code == 200 and thumb.headers.get("content-encoding") is None
    orig = client.get(f"/api/photos/{pid}/download", headers={"Accept-Encoding": "gzip"})
    if orig.status_code == 200:
        assert orig.headers.get("content-encoding") is None


def test_build_files_are_cached_for_good_and_the_page_is_not(client):
    from photointel.api.app import WEB_DIST

    assets = sorted((WEB_DIST / "assets").glob("index-*.js")) if (WEB_DIST / "assets").exists() else []
    if not assets:
        pytest.skip("web/dist is not built")
    r = client.get(f"/assets/{assets[0].name}")
    assert r.status_code == 200
    assert "immutable" in r.headers["cache-control"]
    assert "immutable" not in client.get("/").headers.get("cache-control", "")


def test_merge_suggestions_compare_faces_once_until_something_changes(ctx, monkeypatch):
    conn = ctx.connect()
    for pid, name in ((1, "Asha"), (2, None), (3, "Ravi")):
        conn.execute("INSERT INTO persons(id, name, display_no, created_at, updated_at) VALUES (?,?,?,0,0)",
                     (pid, name, pid))
    conn.commit()
    calls = []
    monkeypatch.setattr(people_mod.db, "active_model_id", lambda c, kind: 1)
    monkeypatch.setattr(people_mod, "load_face_embeddings", lambda c, m: (
        np.arange(6), np.eye(6, 4, dtype=np.float32) + 1, {"person_id": np.array([1, 1, 2, 2, 3, 3])}))

    def fake_suggest(mat, rows, threshold, device):
        calls.append(1)
        return [(1, 2, 0.91)]

    monkeypatch.setattr(people_mod, "suggest_merges", fake_suggest)
    people_mod._merge_pairs_cache.clear()

    first = people_mod.merge_suggestions(ctx, conn)
    second = people_mod.merge_suggestions(ctx, conn)
    assert first == second and len(calls) == 1, "the faces were compared again with nothing changed"

    conn.execute("UPDATE persons SET name = 'Meena' WHERE id = 2")      # a rename: labels are read fresh
    conn.commit()
    renamed = people_mod.merge_suggestions(ctx, conn)
    assert len(calls) == 1 and renamed[0]["b"]["label"] == "Meena"

    conn.execute("INSERT INTO person_not_same(a, b, created_at) VALUES (1, 2, 0)")   # "not the same": fresh too
    conn.commit()
    assert people_mod.merge_suggestions(ctx, conn) == [] and len(calls) == 1
    conn.execute("DELETE FROM person_not_same")
    conn.commit()

    conn.execute("UPDATE persons SET merged_into = 1 WHERE id = 3")      # a merge changes who the faces are
    conn.commit()
    people_mod.merge_suggestions(ctx, conn)
    assert len(calls) == 2, "a merge did not recompute the suggestions"
    conn.close()
