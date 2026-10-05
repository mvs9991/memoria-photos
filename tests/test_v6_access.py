"""Family accounts, the Locked folder and the Archive — the access rules are the point."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages


def index(ctx, root) -> None:
    Indexer(ctx, workers=2).run(roots=[str(root)])


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    return create_app(ctx)


def pid_of(ctx, filename: str) -> int:
    conn = ctx.connect()
    try:
        return int(conn.execute("SELECT id FROM photos WHERE filename = ? ORDER BY id LIMIT 1", (filename,)).fetchone()[0])
    finally:
        conn.close()


def login(app, username, password) -> TestClient:
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def family(ctx, library, app):
    """A library with accounts: an owner, a family member and a guest."""
    index(ctx, library)
    owner = TestClient(app)
    owner.post("/api/auth/password", json={"new": "owner-pass"})
    assert owner.post("/api/accounts/enable", json={"username": "Sanjay"}).status_code == 200
    assert owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"}).status_code == 200
    assert owner.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"}).status_code == 200
    return owner


# ----------------------------------------------------------------- accounts

def test_every_request_needs_an_account(ctx, library, app, family):
    anon = TestClient(app)
    for path in ("/api/stats", "/api/photos/index", "/api/thumb/1", "/api/accounts/me"):
        assert anon.get(path).status_code == 401, path
    assert anon.get("/api/auth/status").json()["accounts"] is True
    assert anon.post("/api/auth/login", json={"username": "Priya", "password": "nope"}).status_code == 401
    assert anon.post("/api/auth/login", json={"username": "sanjay", "password": "owner-pass"}).status_code == 200  # name case
    # the old single-password login no longer opens anything once accounts are on
    assert TestClient(app).post("/api/auth/login", json={"password": "owner-pass"}).status_code == 401


def test_roles(ctx, library, app, family):
    priya, guest = login(app, "Priya", "priya-pass"), login(app, "Guest", "guest-pass")
    pid = pid_of(ctx, "IMG_x0.jpg")
    for c in (priya, guest):
        assert c.get("/api/photos/index").status_code == 200
        assert c.get(f"/api/thumb/{pid}").status_code == 200
        assert c.post("/api/export/zip", data={"spec": f'{{"photo_ids": [{pid}]}}'}).status_code == 200
        for method, path, body in (("post", "/api/trash", {"photo_ids": [pid], "confirm": 1}),
                                   ("post", "/api/settings", {"allow_delete": False}),
                                   ("get", "/api/locked/status", None),
                                   ("post", "/api/backup", {"folder": "x"}),
                                   ("get", "/api/accounts", None),
                                   ("post", "/api/export", {"photo_ids": [pid], "folder": "x"})):
            r = getattr(c, method)(path, **({"json": body} if body else {}))
            assert r.status_code == 403, (c is guest, path, r.status_code)
    # family may organise; a guest may only look
    assert priya.post("/api/photos/rate", json={"photo_ids": [pid], "rating": 4}).status_code == 200
    assert guest.post("/api/photos/rate", json={"photo_ids": [pid], "rating": 1}).status_code == 403
    assert priya.get("/api/accounts/me").json()["username"] == "Priya"
    assert (ctx.paths.data / "library.db").exists()
    assert family.post("/api/trash", json={"photo_ids": [pid], "confirm": 1}).status_code == 200    # the owner can


def test_uploads_are_filed_under_who_sent_them(ctx, library, app, family):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (1, 2, 3)).save(buf, "JPEG")
    priya = login(app, "Priya", "priya-pass")
    r = priya.post("/api/upload", files=[("files", ("a.jpg", buf.getvalue(), "image/jpeg"))]).json()["results"][0]
    assert r["status"] == "added"
    assert "path" not in r                    # the server's folders are not shown to a family member
    stored = ctx.connect().execute("SELECT path FROM uploads WHERE who = 'Priya'").fetchone()[0]
    assert "\\Priya\\" in stored or "/Priya/" in stored


def test_a_password_change_ends_only_that_accounts_sessions(ctx, library, app, family):
    priya, guest = login(app, "Priya", "priya-pass"), login(app, "Guest", "guest-pass")
    uid = next(a["id"] for a in family.get("/api/accounts").json()["accounts"] if a["username"] == "Priya")
    assert family.post(f"/api/accounts/{uid}", json={"password": "new-priya"}).status_code == 200
    assert priya.get("/api/stats").status_code == 401
    assert guest.get("/api/stats").status_code == 200
    assert family.get("/api/stats").status_code == 200
    login(app, "Priya", "new-priya")


def test_accounts_keep_an_owner_and_can_be_switched_off(ctx, library, app, family):
    accts = family.get("/api/accounts").json()["accounts"]
    me = next(a for a in accts if a["username"] == "Sanjay")
    priya = next(a for a in accts if a["username"] == "Priya")
    assert family.post(f"/api/accounts/{me['id']}", json={"role": "family"}).status_code == 400   # last owner
    assert family.delete(f"/api/accounts/{me['id']}").status_code == 400
    assert family.post(f"/api/accounts/{priya['id']}", json={"disabled": True}).status_code == 200
    assert TestClient(app).post("/api/auth/login", json={"username": "Priya", "password": "priya-pass"}).status_code == 401
    assert family.post("/api/accounts", json={"username": "priya", "password": "xxxxxx"}).status_code == 400   # taken


# ----------------------------------------------------------------- the Locked folder

def test_a_locked_photo_is_invisible_until_the_folder_is_opened(ctx, library, app):
    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["people", "events"])
    c = TestClient(app)
    pid = pid_of(ctx, "IMG_x0.jpg")
    face = conn.execute("SELECT id, person_id FROM faces WHERE photo_id = ?", (pid,)).fetchone()
    assert c.post("/api/photos/lock", json={"photo_ids": [pid]}).status_code == 400          # no PIN yet
    assert c.post("/api/locked/pin", json={"new": "2468"}).status_code == 200
    assert c.post("/api/photos/lock", json={"photo_ids": [pid]}).json()["locked"] == 1

    # gone from every list ...
    assert pid not in c.get("/api/photos/index").json()["ids"]
    assert pid not in c.get("/api/photos/index", params={"person": face["person_id"]}).json()["ids"]
    assert pid not in [p["id"] for p in c.get("/api/search", params={"q": "photos"}).json()["photos"]]
    # ... and its pixels, details and faces answer 404
    for path in (f"/api/thumb/{pid}", f"/api/photos/{pid}", f"/api/photos/{pid}/original",
                 f"/api/photos/{pid}/download", f"/api/faces/{face['id']}/crop"):
        assert c.get(path).status_code == 404, path
    assert c.get("/api/locked/photos").status_code == 403
    assert c.post("/api/locked/open", json={"pin": "1111"}).status_code == 401
    assert c.post("/api/locked/open", json={"pin": "2468"}).status_code == 200
    assert c.get("/api/locked/photos").json()["ids"] == [pid]
    assert c.get(f"/api/thumb/{pid}").status_code == 200
    assert c.get(f"/api/photos/{pid}").status_code == 200

    # another browser is not open just because this one is
    other = TestClient(app)
    assert other.get(f"/api/thumb/{pid}").status_code == 404
    # changing the PIN (here, from the other browser) closes it everywhere, this browser included
    other.post("/api/locked/open", json={"pin": "2468"})
    assert other.post("/api/locked/pin", json={"current": "2468", "new": "1357"}).status_code == 200
    assert c.get(f"/api/thumb/{pid}").status_code == 404
    c.post("/api/locked/open", json={"pin": "1357"})
    assert c.post("/api/photos/unlock", json={"photo_ids": [pid]}).json()["unlocked"] == 1
    assert pid in c.get("/api/photos/index").json()["ids"]
    conn.close()


def test_a_locked_photo_stays_locked_through_reindexing(ctx, library, app):
    index(ctx, library)
    c = TestClient(app)
    pid = pid_of(ctx, "IMG_x1.jpg")
    c.post("/api/locked/pin", json={"new": "2468"})
    c.post("/api/photos/lock", json={"photo_ids": [pid]})
    conn = ctx.connect()
    conn.execute("UPDATE photos SET meta_version = 0 WHERE id = ?", (pid,))
    conn.commit()
    index(ctx, library)
    assert conn.execute("SELECT status FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "locked"
    conn.close()


def test_family_cannot_open_the_locked_folder(ctx, library, app, family):
    family.post("/api/locked/pin", json={"new": "2468"})
    pid = pid_of(ctx, "IMG_x2.jpg")
    family.post("/api/photos/lock", json={"photo_ids": [pid]})
    priya = login(app, "Priya", "priya-pass")
    assert priya.post("/api/locked/open", json={"pin": "2468"}).status_code == 403
    # A shared tablet: the owner opened the folder, then Priya logged in on the same browser,
    # which still carries the open-folder cookie. It must not work for her account.
    assert family.post("/api/locked/open", json={"pin": "2468"}).status_code == 200
    assert family.get(f"/api/thumb/{pid}").status_code == 200
    family.post("/api/auth/login", json={"username": "Priya", "password": "priya-pass"})
    assert family.get("/api/accounts/me").json()["username"] == "Priya"
    assert family.get(f"/api/thumb/{pid}").status_code == 404


# ----------------------------------------------------------------- the Archive

def test_archive_leaves_the_timeline_but_not_search_or_albums(ctx, library, app):
    index(ctx, library)
    c = TestClient(app)
    pid = pid_of(ctx, "IMG_x0.jpg")
    album = c.post("/api/albums", json={"name": "Goa", "photo_ids": [pid]}).json()["id"]
    assert c.post("/api/photos/archive", json={"photo_ids": [pid]}).json()["changed"] == 1
    assert pid not in c.get("/api/photos/index", params={"archived": "exclude"}).json()["ids"]
    assert pid in c.get("/api/photos/index").json()["ids"]                     # people, places, folders still see it
    assert c.get("/api/photos/index", params={"collection": "archive"}).json()["ids"] == [pid]
    assert pid in c.get(f"/api/albums/{album}").json()["photos"]["ids"]
    assert pid not in c.get("/api/random", params={"count": 500}).json()["ids"]
    c.post("/api/photos/archive", json={"photo_ids": [pid], "archived": False})
    assert pid in c.get("/api/photos/index", params={"archived": "exclude"}).json()["ids"]
