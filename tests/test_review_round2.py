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


def test_an_export_never_adds_a_live_photos_video_that_is_not_an_ordinary_photo(ctx, client):
    from photointel.engine import export as export_mod

    still, video = visible_ids(ctx, 2)
    c = ctx.connect()
    c.execute("UPDATE photos SET live_video_id = ? WHERE id = ?", (video, still))
    c.execute("UPDATE photos SET status = 'locked', locked = 1 WHERE id = ?", (video,))
    c.commit()
    plan = export_mod.plan(c, export_mod.ExportSpec.from_dict({"photo_ids": [still], "include_live": True}))
    c.close()
    assert [i.photo_id for i in plan.items] == [still], "the locked video half was exported with its still"


def test_removing_a_folder_refreshes_people_counts_and_the_search_index(ctx, client, tmp_path):
    from datetime import datetime

    from photointel import db
    from tests.conftest import make_image

    second = tmp_path / "second"
    for i in range(3):
        make_image(second / f"s{i}.jpg", colour=(30 * i, 200, 90), taken=datetime(2023, 5, 1, 10, i), noise=12)
    Indexer(ctx, workers=1).run(roots=[str(second)])
    c = ctx.connect()
    root_id = c.execute("SELECT id FROM roots WHERE path LIKE ?", (f"%{second.name}",)).fetchone()[0]
    c.close()
    pid = _make_people(ctx, 1, 0)[0]
    c = ctx.connect()
    import time
    import numpy as np
    model = db.active_model_id(c, "face")
    for (photo,) in c.execute("SELECT id FROM photos WHERE root_id = ?", (root_id,)).fetchall():
        c.execute("INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality, embedding, person_id, "
                  "created_at) VALUES (?,?,0.1,0.1,0.4,0.4,0.99,120,0.9,?,?,?)",
                  (photo, model, np.ones(4, np.float16).tobytes(), pid, time.time()))
    from photointel.engine.people import update_person_stats
    update_person_stats(c)
    db.set_meta(c, "post_pending", "0")
    c.commit()
    assert c.execute("SELECT photo_count FROM persons WHERE id = ?", (pid,)).fetchone()[0] == 3
    gen_before = int(db.get_meta(c, "gen:embeddings", 0) or 0)
    c.close()

    assert client.delete(f"/api/roots/{root_id}").status_code == 200
    c = ctx.connect()
    assert c.execute("SELECT photo_count FROM persons WHERE id = ?", (pid,)).fetchone()[0] == 0, \
        "the person still counts photos from the removed folder"
    assert int(db.get_meta(c, "gen:embeddings", 0) or 0) > gen_before, "search still ranks the removed photos"
    assert db.get_meta(c, "post_pending") == "1", "the next scheduled index would skip rebuilding events"
    c.close()


BULK = [("/api/photos/rate", {"rating": 3}), ("/api/photos/hide", {"hidden": True}), ("/api/photos/rotate", {"degrees": 90}),
        ("/api/photos/archive", {"archived": True})]


@pytest.mark.parametrize("path,extra", BULK)
def test_bulk_actions_take_an_empty_selection(ctx, client, path, extra):
    r = client.post(path, json={"photo_ids": [], **extra})
    assert r.status_code in (200, 400), f"{path} with nothing selected: {r.status_code} {r.text[:120]}"


@pytest.mark.parametrize("path,extra", BULK)
def test_bulk_actions_take_more_ids_than_sqlite_binds_at_once(ctx, client, path, extra):
    """"Select all" on a library past ~32,766 photos sends that many ids; one IN (...) cannot hold them."""
    ids = visible_ids(ctx, 2) + list(range(10_000_000, 10_033_000))
    r = client.post(path, json={"photo_ids": ids, **extra})
    assert r.status_code == 200, f"{path} with {len(ids)} ids: {r.status_code} {r.text[:120]}"


def test_assigning_faces_to_a_person_merged_away_gives_them_to_the_survivor(ctx, client):
    a, b = _make_people(ctx, 2, 2)
    client.post("/api/people/merge", json={"target_id": a, "source_ids": [b]})        # b -> a
    c = ctx.connect()
    import time
    import numpy as np
    from photointel import db
    model = db.active_model_id(c, "face")
    photo = visible_ids(ctx, 6)[5]
    fid = c.execute("INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality, embedding, "
                    "created_at) VALUES (?,?,0.5,0.5,0.7,0.7,0.99,120,0.9,?,?)",
                    (photo, model, np.ones(4, np.float16).tobytes(), time.time())).lastrowid
    c.commit()
    c.close()
    client.post("/api/faces/assign", json={"face_ids": [fid], "person_id": b})           # a stale page still shows b
    c = ctx.connect()
    owner = c.execute("SELECT person_id FROM faces WHERE id = ?", (fid,)).fetchone()[0]
    c.close()
    assert owner == a, f"the face went to person {owner}, which was merged away"


def test_splitting_moves_only_faces_that_belong_to_the_person(ctx, client):
    a, b = _make_people(ctx, 2, 2)
    c = ctx.connect()
    faces_a = [r[0] for r in c.execute("SELECT id FROM faces WHERE person_id = ?", (a,))]
    faces_b = [r[0] for r in c.execute("SELECT id FROM faces WHERE person_id = ?", (b,))]
    c.close()
    r = client.post(f"/api/people/{a}/split", json={"face_ids": [faces_a[0], faces_b[0]]}).json()
    c = ctx.connect()
    assert c.execute("SELECT person_id FROM faces WHERE id = ?", (faces_b[0],)).fetchone()[0] == b, \
        "a face of someone else was moved by splitting another person"
    assert c.execute("SELECT person_id FROM faces WHERE id = ?", (faces_a[0],)).fetchone()[0] == r["created"]
    c.close()


def test_a_folder_name_with_an_underscore_does_not_match_its_lookalikes(ctx, client):
    ids = visible_ids(ctx, 4)
    c = ctx.connect()
    root = c.execute("SELECT root_id FROM photos WHERE id = ?", (ids[0],)).fetchone()[0]
    for pid, folder in zip(ids, ("IMG_2020", "IMG_2020/Goa", "IMGX2020", "IMGX2020/Goa")):
        c.execute("UPDATE photos SET folder = ?, root_id = ? WHERE id = ?", (folder, root, pid))
    c.commit()
    c.close()
    got = set(client.get("/api/photos/index", params={"folder": "IMG_2020"}).json()["ids"])
    assert got == set(ids[:2]), f"folder IMG_2020 also matched IMGX2020: {got}"
    sub = client.get("/api/folders/browse", params={"root_id": root, "path": "IMG_2020"}).json()
    assert [f["name"] for f in sub["folders"]] == ["Goa"] and sub["folders"][0]["count"] == 1


def test_random_photos_respect_who_may_see_an_album(ctx, library):
    """/api/random?album= (slideshows, photo frames) took any album id: one family member could pull random
    photos out of another's private album."""
    from photointel.api.app import create_app
    from photointel import ratelimit

    Indexer(ctx, workers=2).run(roots=[str(library)])
    app = create_app(ctx)
    ratelimit.reset_all()
    owner = TestClient(app)
    owner.post("/api/auth/password", json={"new": "owner-pass"})
    assert owner.post("/api/accounts/enable", json={"username": "Sanjay"}).status_code == 200
    owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    owner.post("/api/accounts", json={"username": "Ravi", "password": "ravi-pass", "role": "family"})

    def login(name, pw):
        c = TestClient(app)
        assert c.post("/api/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c

    priya, ravi = login("Priya", "priya-pass"), login("Ravi", "ravi-pass")
    ids = visible_ids(ctx, 2)
    aid = priya.post("/api/albums", json={"name": "Mine", "photo_ids": ids}).json()["id"]
    assert priya.post(f"/api/albums/{aid}", json={"private": True}).status_code == 200
    assert ravi.get(f"/api/albums/{aid}").status_code == 404           # the album itself is hidden from Ravi
    r = ravi.get(f"/api/random?album={aid}&count=5")
    assert r.status_code == 404 or not r.json().get("ids"), "random photos came out of someone else's private album"
    assert priya.get(f"/api/random?album={aid}&count=5").json()["ids"], "the album's owner should still get them"


def test_a_shared_album_does_not_rerun_its_search_or_write_for_every_thumbnail(ctx, client, monkeypatch):
    from photointel.api import routes_auth
    from photointel.api.deps import get_state

    getattr(routes_auth, "_share_ids_cache", {}).clear()
    aid = client.post("/api/albums", json={"name": "Beach", "query": "2024"}).json()["id"]
    token = client.post(f"/api/albums/{aid}/share", json={}).json()["token"]
    ids = client.get(f"/share/{token}".replace("/share", "/api/share")).json()["photos"]["ids"]
    assert ids, "the smart album should hold photos"
    calls = []
    search = get_state().search
    real = search.search
    monkeypatch.setattr(search, "search", lambda *a, **k: calls.append(1) or real(*a, **k))
    writes = []
    c = ctx.connect()
    before = c.execute("SELECT last_used_at FROM share_links WHERE token = ?", (token,)).fetchone()[0]
    c.close()
    for pid in ids[:5]:
        assert client.get(f"/api/share/{token}/thumb/{pid}?s=sm").status_code == 200
    assert len(calls) == 0, f"the album's search ran {len(calls)} times for 5 thumbnails"
    c = ctx.connect()
    after = c.execute("SELECT last_used_at FROM share_links WHERE token = ?", (token,)).fetchone()[0]
    c.close()
    assert after == before, "each thumbnail wrote the link's last-used time"
