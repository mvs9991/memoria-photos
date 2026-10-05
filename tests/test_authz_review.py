"""Leaks found by an authorization review (2026-10-05): what a family member, a guest or a share-link
visitor could see or change that was not theirs. Each test failed before its fix."""
import io

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import rebuild_fts

GOA_LAST = 14


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app
    return create_app(ctx)


def login(app, u, p):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": u, "password": p}).status_code == 200
    return c


@pytest.fixture
def fam(ctx, library, app):
    Indexer(ctx, workers=1).run(roots=[str(library)])
    from photointel.pipeline.post import run_post_stages
    c = ctx.connect()
    run_post_stages(ctx, c)
    c.close()
    owner = TestClient(app)
    owner.post("/api/auth/password", json={"new": "owner-pass"})
    assert owner.post("/api/accounts/enable", json={"username": "Sanjay"}).status_code == 200
    assert owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"}).status_code == 200
    assert owner.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"}).status_code == 200
    return owner, login(app, "Priya", "priya-pass"), login(app, "Guest", "guest-pass")


def lock(owner, ids):
    owner.post("/api/locked/pin", json={"new": "2468"})
    assert owner.post("/api/photos/lock", json={"photo_ids": ids}).status_code == 200


def make_private(ctx, pids, username="Priya"):
    from photointel.engine import visibility
    conn = ctx.connect()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    for pid in pids:
        conn.execute("UPDATE photos SET private_to = ?, status = 'private' WHERE id = ?", (uid, pid))
    conn.commit()
    visibility.refresh(conn, pids)
    conn.close()
    return uid


def test_keyword_search_does_not_return_a_locked_photo(ctx, fam):
    owner, priya, guest = fam
    conn = ctx.connect()
    conn.execute("UPDATE photos SET description = 'the photos of me with him' WHERE id = ?", (GOA_LAST,))
    conn.commit()
    rebuild_fts(conn)              # what the hourly post-processing did before the owner locked it
    conn.close()
    lock(owner, [GOA_LAST])
    for q in ("photos", "the", "me", "with"):
        ids = [p["id"] for p in priya.get("/api/search", params={"q": q}).json()["photos"]]
        assert GOA_LAST not in ids, (q, ids)


# 2 ---------------------------------------------------------------- type-ahead suggestions
def test_suggestions_do_not_reveal_tags_or_places_of_photos_you_cannot_see(ctx, fam):
    owner, priya, guest = fam
    make_private(ctx, [13])
    assert priya.post("/api/photos/tags", json={"photo_ids": [13], "name": "ivf clinic"}).status_code == 200
    sug = owner.get("/api/search/suggestions", params={"q": "ivf"}).json()["suggestions"]
    assert not [s for s in sug if s["type"] == "tag"], sug           # a tag on a private photo, shown to the owner

    conn = ctx.connect()
    pl = conn.execute("INSERT INTO places(name, kind, city, country, lat, lon) "
                      "VALUES ('Baga', 'city', 'Baga', 'India', 15.55, 73.75)").lastrowid
    conn.execute("UPDATE photos SET place_id = ? WHERE id IN (10, 11, 12, 13, 14)", (pl,))
    conn.commit()
    conn.close()
    lock(owner, [10, 11, 12, 14])                                       # 13 is private
    sug = guest.get("/api/search/suggestions", params={"q": "baga"}).json()["suggestions"]
    assert not [s for s in sug if s["type"] == "place"], sug          # "Baga, 5 photos" with 0 visible


# 3 ---------------------------------------------------------------- people seen only in locked photos
def test_a_person_seen_only_in_locked_photos_is_not_named_to_family(ctx, fam):
    owner, priya, guest = fam
    owner.post("/api/people/2/rename", json={"name": "Rahul"})
    lock(owner, [10, 11, 12, 13, 14])                                   # every photo Rahul is in
    assert "Rahul" not in str(priya.get("/api/search/suggestions", params={"q": "rah"}).json())
    assert "Rahul" not in [p["name"] for p in priya.get("/api/people", params={"min_photos": 0}).json()["people"]]
    assert guest.get("/api/people/2").status_code == 404


# 4 ---------------------------------------------------------------- owner-only setting via a side door
def test_family_cannot_change_the_librarys_me_person(ctx, fam):
    owner, priya, guest = fam
    assert priya.post("/api/settings", json={"me_person_id": 1}).status_code == 403   # the front door is shut
    priya.post("/api/people/1/flags", json={"is_me": True})
    assert ctx.settings.me_person_id is None


# 5 ---------------------------------------------------------------- upload de-duplication
def test_uploading_a_copy_does_not_reveal_a_locked_photo(ctx, fam, library):
    owner, priya, guest = fam
    lock(owner, [GOA_LAST])
    data = (library / "Trips/Goa/IMG_x4.jpg").read_bytes()
    res = priya.post("/api/upload", files=[("files", ("mine.jpg", io.BytesIO(data), "image/jpeg"))]).json()["results"][0]
    assert res.get("photo_id") != GOA_LAST, res        # names the locked photo's id ...
    assert res["status"] != "duplicate", res           # ... and silently swallows her own copy


# 6 ---------------------------------------------------------------- writes to photos you cannot see
def test_family_cannot_change_or_probe_a_locked_photo(ctx, fam):
    owner, priya, guest = fam
    lock(owner, [GOA_LAST])
    conn = ctx.connect()
    face = conn.execute("SELECT id FROM faces WHERE photo_id = ?", (GOA_LAST,)).fetchone()[0]
    r = priya.post("/api/photos/rotate", json={"photo_ids": [GOA_LAST, 99999], "degrees": 90}).json()
    assert r["rotated"] == 0, r                                          # 1: an existence oracle for ids
    assert priya.post("/api/photos/hide", json={"photo_ids": [GOA_LAST]}).json()["changed"] == 0
    assert priya.post("/api/photos/archive", json={"photo_ids": [GOA_LAST]}).json()["changed"] == 0
    priya.post("/api/photos/rate", json={"photo_ids": [GOA_LAST], "rating": 1})
    priya.post("/api/photos/tags", json={"photo_ids": [GOA_LAST], "name": "found you"})
    priya.post("/api/faces/assign", json={"face_ids": [face], "name": "Stranger"})
    row = conn.execute("SELECT rotation, hidden, archived, rating FROM photos WHERE id = ?", (GOA_LAST,)).fetchone()
    assert tuple(row) == (0, 0, 0, 0), dict(row)
    assert conn.execute("SELECT COUNT(*) FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
                        "WHERE pt.photo_id = ? AND t.name = 'found you'", (GOA_LAST,)).fetchone()[0] == 0
    assert conn.execute("SELECT p.name FROM faces f LEFT JOIN persons p ON p.id = f.person_id WHERE f.id = ?",
                        (face,)).fetchone()[0] != "Stranger"


# 7 ---------------------------------------------------------------- the Trash is the owner's
def test_a_trashed_photo_is_not_open_to_family_or_guests(ctx, fam):
    owner, priya, guest = fam
    assert owner.post("/api/trash", json={"photo_ids": [13], "confirm": 1}).status_code == 200
    assert priya.get("/api/trash").status_code == 403                   # the Trash page is owner-only ...
    for c in (priya, guest):
        for path in ("/api/photos/13", "/api/thumb/13", "/api/photos/13/original", "/api/photos/13/download"):
            assert c.get(path).status_code == 404, path                 # ... but its photos are not


# 8 ---------------------------------------------------------------- share links run as "no account"
def test_a_shared_smart_album_cannot_reach_into_someone_elses_private_album(ctx, fam, app):
    owner, priya, guest = fam
    pa = priya.post("/api/albums", json={"name": "Therapy", "photo_ids": [10, 11]}).json()["id"]
    priya.post(f"/api/albums/{pa}", json={"private": True})
    sa = owner.post("/api/albums", json={"name": "Everything", "query": "album Therapy"}).json()["id"]
    owner_view = owner.get(f"/api/albums/{sa}").json()["photos"]["ids"]
    token = owner.post(f"/api/albums/{sa}/share", json={}).json()["token"]
    visitor_view = TestClient(app).get(f"/api/share/{token}").json()["photos"]["ids"]
    assert visitor_view == owner_view, (visitor_view, owner_view)      # visitor gets exactly [10, 11]


# 9 ---------------------------------------------------------------- share tokens of an album you cannot see
def test_an_owner_cannot_list_share_links_of_another_owners_private_album(ctx, fam, app):
    owner, priya, guest = fam
    owner.post("/api/accounts", json={"username": "Asha", "password": "asha-pass", "role": "owner"})
    asha = login(app, "Asha", "asha-pass")
    aid = asha.post("/api/albums", json={"name": "Asha only", "photo_ids": [10]}).json()["id"]
    asha.post(f"/api/albums/{aid}", json={"private": True})
    asha.post(f"/api/albums/{aid}/share", json={})
    assert owner.get(f"/api/albums/{aid}").status_code == 404           # invisible to Sanjay ...
    assert owner.get(f"/api/albums/{aid}/shares").status_code == 404    # ... but its live tokens are listed


# 10 --------------------------------------------------------------- photo details name a locked duplicate
def test_photo_details_do_not_count_or_name_a_locked_duplicate(ctx, fam):
    owner, priya, guest = fam
    conn = ctx.connect()
    g = conn.execute("SELECT id, keep_photo_id FROM dup_groups WHERE kind = 'exact'").fetchone()
    other = conn.execute("SELECT photo_id FROM dup_members WHERE group_id = ? AND photo_id != ?",
                         (g["id"], g["keep_photo_id"])).fetchone()[0]
    lock(owner, [g["keep_photo_id"]])
    for d in priya.get(f"/api/photos/{other}").json()["duplicates"]:
        visible = conn.execute("SELECT COUNT(*) FROM dup_members m JOIN photos p ON p.id = m.photo_id "
                               "WHERE m.group_id = ? AND p.status = 'ok'", (d["group_id"],)).fetchone()[0]
        assert d["keep_photo_id"] != g["keep_photo_id"], d             # the locked photo's id, as the keeper
        assert d["count"] == visible, d                                # and counted among the copies


# 11 --------------------------------------------------------------- server paths to non-owners
def test_guests_are_not_told_server_paths(ctx, fam, library):
    owner, priya, guest = fam
    assert guest.get("/api/settings").json()["data_dir"] == ""          # hidden here on purpose ...
    for path in ("/api/stats", "/api/export/location", "/api/folders/browse", "/api/photos/1"):
        body = guest.get(path).text                                     # ... and shown here
        for secret in (str(ctx.paths.data), str(library)):
            assert secret.replace("\\", "\\\\") not in body, path


def test_guests_are_not_shown_job_settings_or_messages(ctx, fam):
    owner, priya, guest = fam
    conn = ctx.connect()
    conn.execute("INSERT INTO jobs(kind, status, params, message, error, created_at) VALUES "
                 "('export', 'done', '{\"folder\": \"X:/secret/exports\"}', 'Copied to X:/secret/exports', NULL, 0)")
    conn.commit()
    body = guest.get("/api/jobs").text
    assert "secret" not in body, body                 # was: the export folder, in params and message
    assert "secret" in owner.get("/api/jobs").text
