"""Bugs found reading the API route by route (2026-10-04). Each test failed before its fix."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer


@pytest.fixture
def client(ctx, library):
    from photointel.api.app import create_app

    Indexer(ctx, workers=2).run(roots=[str(library)])
    return TestClient(create_app(ctx))


def visible_ids(ctx, n=4):
    c = ctx.connect()
    try:
        return [r[0] for r in c.execute("SELECT id FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 "
                                        "AND media_type = 'image' ORDER BY id LIMIT ?", (n,))]
    finally:
        c.close()


def test_an_album_page_does_not_show_a_cover_that_was_hidden_since(ctx, client):
    ids = visible_ids(ctx)
    aid = client.post("/api/albums", json={"name": "Trip", "photo_ids": ids}).json()["id"]
    client.post(f"/api/albums/{aid}", json={"cover_photo_id": ids[0]})
    assert client.get(f"/api/albums/{aid}").json()["cover_photo_id"] == ids[0]
    client.post("/api/photos/hide", json={"photo_ids": [ids[0]], "hidden": True})
    detail = client.get(f"/api/albums/{aid}").json()
    listed = next(a for a in client.get("/api/albums").json()["albums"] if a["id"] == aid)
    assert detail["cover_photo_id"] != ids[0], "the album page still shows the hidden cover"
    assert detail["cover_photo_id"] == listed["cover_photo_id"], "the album page and the album list disagree"


def _make_people(ctx, n_people=2, photos_each=2):
    """People made by hand (the test library's fake detector finds no faces): each gets faces on its own photos."""
    import time

    import numpy as np

    from photointel import db
    from photointel.engine.people import create_person, update_person_stats

    c = ctx.connect()
    try:
        model = db.register_model(c, "face", "test-face", "1", 4, {})
        db.set_active_model(c, "face", model)
        photos = [r[0] for r in c.execute("SELECT id FROM photos WHERE status = 'ok' AND hidden = 0 AND "
                                          "live_component = 0 ORDER BY id LIMIT ?", (n_people * photos_each,))]
        people = []
        for k in range(n_people):
            pid = create_person(c, f"P{k}", face_model=model)
            people.append(pid)
            for ph in photos[k * photos_each:(k + 1) * photos_each]:
                emb = np.ones(4, np.float16).tobytes()
                c.execute("INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality, embedding, "
                          "person_id, created_at) VALUES (?,?,0.1,0.1,0.4,0.4,0.99,120,0.9,?,?,?)",
                          (ph, model, emb, pid, time.time()))
        update_person_stats(c)
        c.commit()
        return people
    finally:
        c.close()


def test_a_persons_best_photos_and_faces_leave_out_hidden_photos(ctx, client):
    pid = _make_people(ctx, 1, 3)[0]
    c = ctx.connect()
    photo = c.execute("SELECT photo_id FROM faces WHERE person_id = ? LIMIT 1", (pid,)).fetchone()[0]
    c.close()
    client.post("/api/photos/hide", json={"photo_ids": [photo], "hidden": True})
    detail = client.get(f"/api/people/{pid}").json()
    assert photo not in detail["representative_photos"], "a hidden photo is among the person's best photos"
    faces = client.get(f"/api/people/{pid}/faces").json()["faces"]
    assert photo not in {f["photo_id"] for f in faces}, "a hidden photo's face is listed for review"


def test_merging_into_a_person_already_merged_away_cannot_lose_faces_or_loop(ctx, client):
    a, b = _make_people(ctx, 2, 2)
    client.post("/api/people/merge", json={"target_id": a, "source_ids": [b]})      # b -> a
    client.post("/api/people/merge", json={"target_id": b, "source_ids": [a]})      # a stale page: a -> b
    r = client.get(f"/api/people/{a}")
    assert r.status_code == 200, r.text[:200]
    c = ctx.connect()
    live = {row[0] for row in c.execute("SELECT id FROM persons WHERE merged_into IS NULL")}
    orphaned = c.execute("SELECT COUNT(*) FROM faces f JOIN persons p ON p.id = f.person_id "
                         "WHERE p.merged_into IS NOT NULL").fetchone()[0]
    c.close()
    assert a in live or b in live, "both people were merged away"
    assert orphaned == 0, f"{orphaned} faces belong to a person that was merged away"
