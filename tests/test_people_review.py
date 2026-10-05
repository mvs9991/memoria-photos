"""People and faces bugs found by a review (2026-10-05). Each failed before its fix."""
import datetime as _dt
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photointel import db
from photointel.engine import people as pm
from photointel.pipeline.indexer import Indexer


@pytest.fixture
def conn(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    c = ctx.connect()
    pm.recluster(ctx, c)
    return c


@pytest.fixture
def client(ctx, conn):
    from photointel.api.app import create_app
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def biggest(c, n=1):
    ids = [r[0] for r in c.execute("SELECT id FROM persons WHERE face_count > 0 ORDER BY face_count DESC, id")]
    return ids[:n] if n > 1 else ids[0]


def faces_of(c, pid):
    return [r[0] for r in c.execute("SELECT id FROM faces WHERE person_id = ? ORDER BY id", (pid,))]


# 1
@pytest.mark.parametrize("how", ["split", "assign_new_name"])
def test_full_recluster_keeps_a_named_persons_faces_after_a_correction(ctx, conn, how):
    pid = biggest(conn)
    pm.rename_person(conn, pid, "Priya")
    faces = faces_of(conn, pid)
    if how == "split":
        pm.split_person(conn, pid, faces[:2])
    else:
        pm.assign_faces(conn, faces[:2], name="Ravi")
    pm.recluster(ctx, conn, full=True)
    assert len(faces_of(conn, pid)) == len(faces) - 2, "Priya's other faces went to the corrected person"


# 2
def test_reanalysing_a_changed_photo_keeps_not_this_person(ctx, conn, library):
    pid = biggest(conn)
    fid, photo = conn.execute("SELECT id, photo_id FROM faces WHERE person_id = ? ORDER BY id LIMIT 1", (pid,)).fetchone()
    pm.reject_faces(conn, [fid], pid)
    row = conn.execute("SELECT folder, filename FROM photos WHERE id = ?", (photo,)).fetchone()
    path = library / row["folder"] / row["filename"]
    im = Image.open(path)
    im.save(path, "JPEG", quality=80, exif=im.info.get("exif", b""))       # an editor re-saved it
    Indexer(ctx, workers=2).run(roots=[str(library)])
    pm.recluster(ctx, conn)
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE photo_id = ? AND person_id = ?", (photo, pid)).fetchone()[0] == 0


# 3
def test_a_face_model_upgrade_keeps_not_this_person(ctx, conn):
    pid = biggest(conn)
    fid, photo = conn.execute("SELECT id, photo_id FROM faces WHERE person_id = ? ORDER BY id LIMIT 1", (pid,)).fetchone()
    pm.reject_faces(conn, [fid], pid)
    old = db.active_model_id(conn, "face")
    new = db.register_model(conn, "face", "next-gen-face", "2", 32)
    db.set_active_model(conn, "face", new)
    conn.execute("INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality, embedding, created_at) "
                 "SELECT photo_id, ?, x1, y1, x2, y2, det_score, size_px, quality, embedding, 0 FROM faces WHERE model_id = ?",
                 (new, old))
    conn.commit()
    pm.migrate_face_identities(conn, old, new)
    pm.prune_stale_face_models(conn, new)
    pm.recluster(ctx, conn)
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE photo_id = ? AND person_id = ?", (photo, pid)).fetchone()[0] == 0


# 4
def test_same_person_keeps_the_name_whichever_side_has_it(conn, client):
    a, b = sorted(biggest(conn, 2))
    client.post(f"/api/people/{b}/rename", json={"name": "Priya"})
    client.post("/api/people/merge", json={"target_id": a, "source_ids": [b]})   # the suggestion card: target = smaller id
    assert "Priya" in [p["name"] for p in client.get("/api/people").json()["people"]]


# 5
def test_me_follows_a_merge(conn, client):
    a, b = biggest(conn, 2)
    client.post(f"/api/people/{b}/flags", json={"is_me": True})
    before = client.get("/api/search", params={"q": "photos of me"}).json()["total"]
    client.post("/api/people/merge", json={"target_id": a, "source_ids": [b]})
    assert client.get("/api/search", params={"q": "photos of me"}).json()["total"] >= before > 0
    assert client.get(f"/api/people/{a}").json()["is_me"]


# 6
def test_a_stale_page_after_a_merge_still_renames_and_rejects(conn, client):
    a, b = biggest(conn, 2)
    fb = faces_of(conn, b)[0]
    client.post("/api/people/merge", json={"target_id": a, "source_ids": [b]})
    client.post(f"/api/people/{b}/rename", json={"name": "Priya"})
    client.post("/api/faces/reject", json={"face_ids": [fb], "person_id": b})
    assert client.get(f"/api/people/{b}").json()["name"] == "Priya"
    assert conn.execute("SELECT person_id FROM faces WHERE id = ?", (fb,)).fetchone()[0] is None


# 7
def test_naming_faces_finds_the_existing_person(conn, client):
    a, b = biggest(conn, 2)
    client.post(f"/api/people/{a}/rename", json={"name": "Priya"})
    r = client.post("/api/faces/assign", json={"face_ids": [faces_of(conn, b)[0]], "name": "priya "}).json()
    assert r["person_id"] == a
    r = client.post("/api/faces/assign", json={"face_ids": [faces_of(conn, b)[0]], "name": "   "}).json()
    assert conn.execute("SELECT name FROM persons WHERE id = ?", (r["person_id"],)).fetchone()[0] is None


# 8
def test_unknown_person_is_404_and_does_not_lock_the_database(ctx, conn, client):
    face = faces_of(conn, biggest(conn))[0]
    for path, body in [("/api/people/merge", {"target_id": 99999, "source_ids": [biggest(conn)]}),
                       ("/api/faces/assign", {"face_ids": [face], "person_id": 99999}),
                       ("/api/faces/reject", {"face_ids": [face], "person_id": 99999}),
                       ("/api/people/99999/rename", {"name": "X"})]:
        assert client.post(path, json=body).status_code == 404, path
        other = ctx.connect()
        other.execute("PRAGMA busy_timeout = 500")
        other.execute("UPDATE persons SET updated_at = updated_at")    # raises "database is locked" today
        other.commit()
        other.close()


# 9
def test_birth_dates_are_stored_canonically_and_not_in_the_future(conn, client, monkeypatch):
    from photointel.api import routes_library

    class Now(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 3, 8, 12)
    monkeypatch.setattr(routes_library, "datetime", Now)
    pid = conn.execute("SELECT f.person_id FROM faces f JOIN photos p ON p.id = f.photo_id "
                       "WHERE date(p.taken_ts, 'unixepoch') = '2024-03-09' AND f.person_id IS NOT NULL").fetchone()[0]
    assert client.post(f"/api/people/{pid}/flags", json={"birth_date": "2000-3-9"}).status_code == 200
    assert conn.execute("SELECT birth_date FROM persons WHERE id = ?", (pid,)).fetchone()[0] == "2000-03-09"
    assert any(s["kind"] == "birthday" for s in client.get("/api/memories").json()["sections"])
    assert client.post(f"/api/people/{pid}/flags", json={"birth_date": "2099-03-09"}).status_code == 400


def _lookalikes(ctx, library, n):
    Indexer(ctx, workers=2, enable_faces=False).run(roots=[str(library)])
    c = ctx.connect()
    model = db.register_model(c, "face", "test-face", "1", 4, {})
    db.set_active_model(c, "face", model)
    photos = [r[0] for r in c.execute("SELECT id FROM photos WHERE status = 'ok' AND live_component = 0")]
    rng, people = np.random.default_rng(0), []
    base = rng.normal(size=4)
    for k in range(n):
        people.append(pm.create_person(c, None, face_model=model))
        for j in range(2):
            v = base + rng.normal(scale=0.05, size=4)
            c.execute("INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality, embedding, "
                      "person_id, created_at) VALUES (?,?,0.1,0.1,0.4,0.4,0.99,120,0.9,?,?,0)",
                      (photos[(2 * k + j) % len(photos)], model, (v / np.linalg.norm(v)).astype(np.float16).tobytes(),
                       people[-1]))
    pm.update_person_stats(c)
    c.commit()
    return c, people


class _Ctx:
    device = "cpu"


# 10
def test_dismissing_the_shown_suggestions_reveals_the_next_ones(ctx, library):
    c, people = _lookalikes(ctx, library, 20)
    for s in pm.merge_suggestions(_Ctx(), c, limit=100):
        pm.mark_not_same(c, s["a"]["id"], s["b"]["id"])
    undecided = 190 - c.execute("SELECT COUNT(*) FROM person_not_same").fetchone()[0]
    assert undecided > 100 and pm.merge_suggestions(_Ctx(), c, limit=100)


# 11
def test_different_people_stay_different_after_a_merge(ctx, library):
    c, (a, b, x) = _lookalikes(ctx, library, 3)
    pm.mark_not_same(c, a, x)
    pm.merge_persons(c, b, [a])
    assert not any({s["a"]["id"], s["b"]["id"]} == {b, x} for s in pm.merge_suggestions(_Ctx(), c))


# 12
def test_appears_with_leaves_out_hidden_photos(ctx, library):
    Indexer(ctx, workers=2, enable_faces=False).run(roots=[str(library)])
    c = ctx.connect()
    model = db.register_model(c, "face", "test-face", "1", 4, {})
    db.set_active_model(c, "face", model)
    p1, p2 = [r[0] for r in c.execute("SELECT id FROM photos WHERE status = 'ok' AND live_component = 0 LIMIT 2")]
    a, b = pm.create_person(c, "A", face_model=model), pm.create_person(c, "B", face_model=model)
    for ph, pid in [(p1, a), (p1, b), (p2, a)]:
        c.execute("INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality, embedding, "
                  "person_id, created_at) VALUES (?,?,0.1,0.1,0.4,0.4,0.99,120,0.9,?,?,0)",
                  (ph, model, np.ones(4, np.float16).tobytes(), pid))
    pm.update_person_stats(c)
    c.commit()
    from photointel.api.app import create_app
    client = TestClient(create_app(ctx))
    client.post("/api/photos/hide", json={"photo_ids": [p1], "hidden": True})
    assert client.get(f"/api/people/{a}").json()["co_occurring"] == []


# 13
def test_opposite_merges_at_the_same_moment_do_not_orphan_faces(ctx, conn, monkeypatch):
    a, b = biggest(conn, 2)
    read, merged = threading.Event(), threading.Event()
    real = pm.live_person

    def slow(c, pid):
        out = real(c, pid)
        if threading.current_thread().name == "second":
            read.set()
            merged.wait(3)                     # the other request commits between this read and the writes
        return out
    monkeypatch.setattr(pm, "live_person", slow)

    def second():
        c2 = ctx.connect()
        pm.merge_persons(c2, a, [b])
        c2.close()
    t = threading.Thread(target=second, name="second")
    t.start()
    read.wait(3)
    c1 = ctx.connect()
    pm.merge_persons(c1, b, [a])
    c1.close()
    merged.set()
    t.join()
    assert conn.execute("SELECT COUNT(*) FROM faces f JOIN persons p ON p.id = f.person_id "
                        "WHERE p.merged_into IS NOT NULL").fetchone()[0] == 0
