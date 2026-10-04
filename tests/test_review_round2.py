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


def test_an_event_page_leaves_out_hidden_photos(ctx, client):
    """A guard, not a regression test: this already held (hiding a photo detaches it from its event, see
    visibility.detach_unseen), but the event page's own query does not filter hidden photos, so keep it so."""
    import time

    ids = visible_ids(ctx, 4)
    c = ctx.connect()
    now = time.time()
    eid = c.execute("INSERT INTO events(kind, start_ts, end_ts, photo_count, people_count, auto_title, created_at, "
                    "updated_at) VALUES ('event', ?, ?, 4, 0, 'Picnic', ?, ?)", (now - 3600, now, now, now)).lastrowid
    c.execute(f"UPDATE photos SET event_id = ? WHERE id IN ({','.join(map(str, ids))})", (eid,))
    c.commit()
    c.close()
    photo = ids[0]
    client.post("/api/photos/hide", json={"photo_ids": [photo], "hidden": True})
    detail = client.get(f"/api/events/{eid}").json()
    assert photo not in detail["photos"]["ids"], "a hidden photo is in the event's grid"
    assert photo not in detail["highlights"]
    assert detail["photos"]["total"] == client.get(f"/api/photos/index?event={eid}").json()["total"]


def test_a_big_events_highlights_come_from_all_of_it(ctx, client):
    import time

    c = ctx.connect()
    base = c.execute("SELECT * FROM photos WHERE status = 'ok' AND media_type = 'image' LIMIT 1").fetchone()
    now = time.time()
    eid = c.execute("INSERT INTO events(kind, start_ts, end_ts, photo_count, people_count, auto_title, created_at, "
                    "updated_at) VALUES ('event', ?, ?, 1000, 0, 'Wedding', ?, ?)", (now - 9000, now, now, now)).lastrowid
    cols = [k for k in base.keys() if k not in ("id", "rel_path", "filename", "sha256", "event_id", "taken_ts",
                                                 "quality_score", "rating")]
    for i in range(1000):          # 1,000 photos; only the very last one is rated
        c.execute(f"INSERT INTO photos({','.join(cols)}, rel_path, filename, sha256, event_id, taken_ts, quality_score, rating) "
                  f"VALUES ({','.join('?' * len(cols))}, ?, ?, ?, ?, ?, ?, ?)",
                  (*[base[k] for k in cols], f"w/{i}.jpg", f"{i}.jpg", f"sha{i:060d}", eid, now - 9000 + i, 0.1,
                   5 if i == 999 else 0))
    c.commit()
    last = c.execute("SELECT id FROM photos WHERE rel_path = 'w/999.jpg'").fetchone()[0]
    c.close()
    detail = client.get(f"/api/events/{eid}").json()
    assert detail["highlights"][0] == last, "the best photo of the event's second half was never considered"


def _dup_group(ctx, n=3):
    import time

    ids = visible_ids(ctx, n + 1)
    c = ctx.connect()
    now = time.time()
    gid = c.execute("INSERT INTO dup_groups(kind, keep_photo_id, member_count, review_status, signature, created_at, "
                    "updated_at) VALUES ('exact', ?, ?, 'pending', ?, ?, ?)", (ids[0], n, f"test-{now}", now, now)).lastrowid
    for pid in ids[:n]:
        c.execute("INSERT INTO dup_members(group_id, photo_id, relation, similarity) VALUES (?, ?, 'exact', 1.0)", (gid, pid))
    c.commit()
    c.close()
    return gid, ids[:n], ids[n]          # the group, its members (first is the keeper), a photo outside it


def test_hide_copies_never_hides_the_keeper_or_a_photo_outside_the_group(ctx, client):
    gid, members, outsider = _dup_group(ctx)
    r = client.post(f"/api/duplicates/{gid}/hide-copies", json={"photo_ids": members + [outsider]})
    assert r.status_code == 200
    c = ctx.connect()
    hidden = {row[0] for row in c.execute("SELECT id FROM photos WHERE hidden = 1")}
    c.close()
    assert members[0] not in hidden, "the copy marked keep was hidden"
    assert outsider not in hidden, "a photo from outside the group was hidden"
    assert set(members[1:]) <= hidden


def test_a_review_accepts_only_known_states_and_a_keeper_from_the_group(ctx, client):
    gid, members, outsider = _dup_group(ctx)
    assert client.post(f"/api/duplicates/{gid}/review", json={"status": "banana"}).status_code == 422
    assert client.post(f"/api/duplicates/{gid}/review", json={"keep_photo_id": outsider}).status_code == 400
    assert client.post(f"/api/duplicates/{gid}/review", json={"keep_photo_id": members[1], "status": "reviewed"}).status_code == 200
