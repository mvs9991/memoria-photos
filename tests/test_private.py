"""Private photos: a family member's phone backups, seen by them alone (engine/private.py)."""
from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photointel.pipeline.indexer import Indexer

PRIYA = ("Priya", "priya-pass")


def _jpeg(colour=(10, 120, 200), taken="2023:08:15 10:30:00", size=(320, 240)) -> bytes:
    import piexif

    img = Image.new("RGB", size, colour)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=piexif.dump({"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: taken.encode()},
                                            "GPS": {}}))
    return buf.getvalue()


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    return create_app(ctx)


@pytest.fixture
def family(app):
    owner = TestClient(app)
    owner.post("/api/auth/password", json={"new": "owner-pass"})
    owner.post("/api/accounts/enable", json={"username": "Sanjay"})
    owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    owner.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"})
    priya = TestClient(app)
    priya.post("/api/auth/login", json={"username": "Priya", "password": "priya-pass"})
    return owner, priya


def index_uploads(ctx):
    Indexer(ctx, workers=1).run(roots=[str(ctx.paths.data / "uploads")])


def pid_of(ctx, name: str) -> int:
    conn = ctx.connect()
    row = conn.execute("SELECT id FROM photos WHERE filename = ?", (name,)).fetchone()
    conn.close()
    return int(row[0])


def sees(client: TestClient, pid: int) -> dict:
    """Every way a photo can show up for this person."""
    return {
        "timeline": pid in client.get("/api/photos/index").json()["ids"],
        "details": client.get(f"/api/photos/{pid}").status_code == 200,
        "thumb": client.get(f"/api/thumb/{pid}?s=m").status_code == 200,
        "original": client.get(f"/api/photos/{pid}/original").status_code == 200,
        "search": pid in [p["id"] for p in client.get("/api/search", params={"q": "2023"}).json()["photos"]],
        "private_page": pid in client.get("/api/private/photos").json()["ids"],
    }


NOWHERE = {"timeline": False, "details": False, "thumb": False, "original": False, "search": False,
           "private_page": False}
SHARED = {**NOWHERE, "timeline": True, "details": True, "thumb": True, "original": True, "search": True}
ITS_PERSON = {**NOWHERE, "details": True, "thumb": True, "original": True, "private_page": True}


def _put(app, name, data, auth=PRIYA):
    r = TestClient(app).put(f"/dav/Camera/{name}", content=data, auth=auth)
    assert r.status_code in (201, 204), r.text


def test_a_private_backup_is_seen_by_its_person_only(ctx, app, family):
    owner, priya = family
    assert priya.post("/api/accounts/me/private-uploads", json={"on": True}).status_code == 200
    _put(app, "IMG_1.jpg", _jpeg())
    index_uploads(ctx)
    pid = pid_of(ctx, "IMG_1.jpg")
    assert sees(owner, pid) == NOWHERE                       # not even the library's owner
    assert sees(priya, pid) == ITS_PERSON                    # on her Private page, not in the shared timeline
    assert priya.get(f"/api/thumb/{pid}?s=m").headers["cache-control"] == "no-store"   # not in phones' offline copy
    assert owner.get("/api/stats").json()["photos"] == 0
    guest = TestClient(app)
    guest.post("/api/auth/login", json={"username": "Guest", "password": "guest-pass"})
    assert sees(guest, pid) == NOWHERE

    # the owner's Locked folder never shows it either
    owner.post("/api/locked/pin", json={"new": "2468"})
    owner.post("/api/locked/open", json={"pin": "2468"})
    assert pid not in owner.get("/api/locked/photos").json()["ids"]
    assert sees(owner, pid)["details"] is False


def test_it_stays_private_through_changes_and_a_drive_coming_back(ctx, app, family):
    owner, priya = family
    priya.post("/api/accounts/me/private-uploads", json={"on": True})
    _put(app, "IMG_1.jpg", _jpeg())
    # a second photo stays: a folder that suddenly looks empty is taken for an unplugged drive,
    # and nothing in it is marked missing
    _put(app, "IMG_stays.jpg", _jpeg((5, 5, 250), taken="2022:01:01 09:00:00"))
    index_uploads(ctx)
    pid = pid_of(ctx, "IMG_1.jpg")
    stored = next((ctx.paths.data / "uploads" / "Priya").rglob("IMG_1.jpg"))

    stored.write_bytes(_jpeg((200, 40, 40)))                 # edited on disk: re-analysed
    os.utime(stored, (time.time() + 5, time.time() + 5))
    index_uploads(ctx)
    assert sees(owner, pid) == NOWHERE and sees(priya, pid)["private_page"]

    away = stored.with_name("away.tmp")                      # gone and back
    stored.rename(away)
    index_uploads(ctx)
    conn = ctx.connect()
    assert conn.execute("SELECT status FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "missing"
    conn.close()
    away.rename(stored)
    index_uploads(ctx)
    assert sees(owner, pid) == NOWHERE and sees(priya, pid)["private_page"]


def test_sharing_back_and_making_earlier_uploads_private(ctx, app, family):
    owner, priya = family
    _put(app, "IMG_old.jpg", _jpeg((40, 200, 40)))           # before she turned it on: shared
    index_uploads(ctx)
    old = pid_of(ctx, "IMG_old.jpg")
    assert sees(owner, old) == SHARED
    priya.post("/api/accounts/me/private-uploads", json={"on": True})
    index_uploads(ctx)
    assert sees(owner, old) == SHARED                        # turning it on changes only what comes next

    r = priya.post("/api/photos/private", json={})          # "make my earlier uploads private"
    assert r.json()["private"] == 1
    assert sees(owner, old) == NOWHERE
    assert priya.post("/api/photos/share-with-family", json={"photo_ids": [old]}).json()["shared"] == 1
    assert sees(owner, old) == SHARED
    # someone else's photo can't be made private by her, and she can't share one that isn't hers
    _put(app, "IMG_sanjay.jpg", _jpeg((90, 90, 250)), auth=("Sanjay", "owner-pass"))
    index_uploads(ctx)
    his = pid_of(ctx, "IMG_sanjay.jpg")
    assert priya.post("/api/photos/private", json={"photo_ids": [his]}).json() == {"private": 0, "not_yours": 1}
    assert sees(owner, his) == SHARED
    owner.post("/api/photos/private", json={"photo_ids": [his]})
    assert priya.post("/api/photos/share-with-family", json={"photo_ids": [his]}).json()["shared"] == 0
    assert sees(priya, his) == NOWHERE


def test_someone_elses_private_copy_does_not_swallow_an_upload(ctx, app, family):
    owner, priya = family
    priya.post("/api/accounts/me/private-uploads", json={"on": True})
    first, second = _jpeg((123, 45, 67)), _jpeg((67, 45, 123))
    _put(app, "IMG_1.jpg", first)
    # her copy not indexed yet: his arrives
    r = owner.post("/api/upload", files={"files": ("mine.jpg", first, "image/jpeg")})
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "added", r.text
    _put(app, "IMG_2.jpg", second)
    index_uploads(ctx)
    # her copy indexed (and private): his still arrives
    r = owner.post("/api/upload", files={"files": ("mine2.jpg", second, "image/jpeg")})
    assert r.json()["results"][0]["status"] == "added", r.text
    index_uploads(ctx)
    r = owner.post("/api/upload", files={"files": ("mine-again.jpg", first, "image/jpeg")})
    assert r.json()["results"][0]["status"] == "duplicate"          # his own copy is there now
    assert sees(owner, pid_of(ctx, "mine.jpg"))["timeline"] and sees(owner, pid_of(ctx, "mine2.jpg"))["timeline"]
    assert sees(owner, pid_of(ctx, "IMG_1.jpg")) == NOWHERE and sees(owner, pid_of(ctx, "IMG_2.jpg")) == NOWHERE


def test_a_removed_account_hands_its_private_photos_to_the_owner_not_everyone(ctx, app, family):
    owner, priya = family
    priya.post("/api/accounts/me/private-uploads", json={"on": True})
    _put(app, "IMG_1.jpg", _jpeg())
    index_uploads(ctx)
    pid = pid_of(ctx, "IMG_1.jpg")
    uid = next(a["id"] for a in owner.get("/api/accounts").json()["accounts"] if a["username"] == "Priya")
    assert owner.delete(f"/api/accounts/{uid}").status_code == 200
    assert sees(owner, pid) == ITS_PERSON


def test_guests_and_libraries_without_accounts(ctx, app, family):
    owner, _ = family
    guest = TestClient(app)
    guest.post("/api/auth/login", json={"username": "Guest", "password": "guest-pass"})
    assert guest.post("/api/photos/private", json={}).status_code == 403


def test_without_accounts_there_is_nothing_private(ctx, app):
    c = TestClient(app)
    assert c.post("/api/photos/private", json={}).status_code == 400
    assert c.get("/api/private/photos").json()["ids"] == []


def test_private_beats_locked_whenever_a_status_is_decided(ctx, app, family):
    """Not reachable from the app today (only visible photos can be locked or made private), but if
    both flags are ever set the photo must stay its person's, never land in the owner's Locked folder."""
    owner, priya = family
    priya.post("/api/accounts/me/private-uploads", json={"on": True})
    _put(app, "IMG_1.jpg", _jpeg())
    _put(app, "IMG_stays.jpg", _jpeg((5, 5, 250), taken="2022:01:01 09:00:00"))
    index_uploads(ctx)
    pid = pid_of(ctx, "IMG_1.jpg")
    conn = ctx.connect()
    conn.execute("UPDATE photos SET locked = 1 WHERE id = ?", (pid,))
    conn.commit()
    stored = next((ctx.paths.data / "uploads" / "Priya").rglob("IMG_1.jpg"))
    stored.write_bytes(_jpeg((200, 40, 40)))
    os.utime(stored, (time.time() + 5, time.time() + 5))
    index_uploads(ctx)                                        # re-analysed
    assert conn.execute("SELECT status FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "private"
    away = stored.with_name("away.tmp")
    stored.rename(away)
    index_uploads(ctx)
    away.rename(stored)
    index_uploads(ctx)                                        # missing and back
    assert conn.execute("SELECT status FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "private"
    conn.close()


def test_nothing_the_owner_can_open_mentions_a_private_photo(ctx, app, family, library):
    """A sweep: every list the app offers, as the owner, and each person, album and event in them.
    None may name the photo, carry its fingerprint or show Priya's private folder."""
    from photointel.pipeline.indexer import Indexer

    owner, priya = family
    Indexer(ctx, workers=1).run(roots=[str(library)])           # a real library around it
    priya.post("/api/accounts/me/private-uploads", json={"on": True})
    _put(app, "IMG_private_1.jpg", _jpeg((250, 200, 10)))
    index_uploads(ctx)
    pid = pid_of(ctx, "IMG_private_1.jpg")
    conn = ctx.connect()
    sha = conn.execute("SELECT sha256 FROM photos WHERE id = ?", (pid,)).fetchone()[0]
    face_ids = [r[0] for r in conn.execute("SELECT id FROM faces WHERE photo_id = ?", (pid,))]
    conn.close()
    markers = ("IMG_private_1", sha, "Priya/2023")

    skip = ("/api/random/image", "/api/auth/", "/api/share/", "/api/openapi", "/api/docs", "/api/alerts",
            "/api/https", "/api/offsite", "/api/export/")
    paths = [p for p, ops in app.openapi()["paths"].items()
             if "get" in ops and "{" not in p and not p.startswith(skip)]
    people = [p["id"] for p in owner.get("/api/people").json().get("people", [])]
    albums = [a["id"] for a in owner.get("/api/albums").json().get("albums", [])]
    events = [e["id"] for e in owner.get("/api/events").json().get("events", [])]
    paths += [f"/api/people/{i}" for i in people] + [f"/api/people/{i}/faces" for i in people]
    paths += [f"/api/albums/{i}" for i in albums] + [f"/api/events/{i}" for i in events]
    paths += ["/api/search?q=2023", "/api/search?q=yellow", "/api/photos/index?year=2023"]
    checked = 0
    for path in paths:
        r = owner.get(path)
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            continue
        checked += 1
        body = r.text
        for m in markers:
            assert m not in body, f"{path} mentions the private photo ({m})"
        ids = r.json().get("ids") if isinstance(r.json(), dict) else None
        assert not ids or pid not in ids, path
    assert checked > 30, checked                                  # the sweep really ran
    for fid in face_ids:
        assert owner.get(f"/api/faces/{fid}/crop").status_code == 404
