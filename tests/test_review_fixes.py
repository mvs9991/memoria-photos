"""Regression tests for bugs found in the deep review of the v4–v6 work. Each one failed
against the code before its fix."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photointel.engine.export import ExportSpec, export_to_folder
from photointel.engine.xmp import ExportError, export_xmp
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


# 1 ---------------------------------------------------------------- a deleted account's cookie

def test_a_deleted_accounts_cookie_never_signs_in_as_the_next_account(ctx, library, app):
    owner = TestClient(app)
    owner.post("/api/auth/password", json={"new": "owner-pass"})
    owner.post("/api/accounts/enable", json={"username": "Sanjay"})
    owner.post("/api/accounts", json={"username": "Bob", "password": "bob-pass", "role": "guest"})
    bob = TestClient(app)
    assert bob.post("/api/auth/login", json={"username": "Bob", "password": "bob-pass"}).status_code == 200
    bob_id = next(a["id"] for a in owner.get("/api/accounts").json()["accounts"] if a["username"] == "Bob")
    owner.delete(f"/api/accounts/{bob_id}")
    owner.post("/api/accounts", json={"username": "Mum", "password": "mum-pass", "role": "family"})
    r = bob.get("/api/accounts/me")
    assert r.status_code == 401, r.json()


# 2 ---------------------------------------------------------------- exporting into a root's parent

def test_export_into_a_parent_of_the_library_never_writes_inside_it(ctx, library):
    index(ctx, library)
    before = {p for p in library.rglob("*")}
    conn = ctx.connect()
    with pytest.raises(ExportError):
        export_to_folder(conn, ExportSpec(year=2024, month=7, layout="original", xmp=True, folder=str(library.parent)))
    with pytest.raises(ExportError):
        export_xmp(conn, library.parent, everything=True)
    assert {p for p in library.rglob("*")} == before
    # the date layout into the same parent is fine: nothing lands in the library
    out = export_to_folder(conn, ExportSpec(year=2024, month=7, layout="date", folder=str(library.parent / "out")))
    assert out["copied"] == 5
    conn.close()


# 3 ---------------------------------------------------------------- browsing the server's disks

def test_only_the_owner_may_browse_the_servers_folders(ctx, library, app):
    owner = TestClient(app)
    owner.post("/api/auth/password", json={"new": "owner-pass"})
    owner.post("/api/accounts/enable", json={"username": "Sanjay"})
    owner.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"})
    owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    for name, pw in (("Guest", "guest-pass"), ("Priya", "priya-pass")):
        c = TestClient(app)
        c.post("/api/auth/login", json={"username": name, "password": pw})
        assert c.get("/api/browse", params={"path": str(library)}).status_code == 403, name
    assert owner.get("/api/browse", params={"path": str(library)}).status_code == 200


# 4 ---------------------------------------------------------------- locked photos leak through side doors

def test_locked_photos_do_not_leak_through_describe_similar_or_flags(ctx, library, app):
    index(ctx, library)
    c = TestClient(app)
    pid = pid_of(ctx, "IMG_x0.jpg")
    conn = ctx.connect()
    conn.execute("UPDATE photos SET caption = 'a private caption' WHERE id = ?", (pid,))
    conn.commit()
    c.post("/api/locked/pin", json={"new": "2468"})
    c.post("/api/photos/lock", json={"photo_ids": [pid]})
    assert c.post(f"/api/photos/{pid}/describe").status_code == 404
    assert c.get(f"/api/photos/{pid}/similar").status_code == 404
    assert c.post(f"/api/photos/{pid}/flags", json={"favorite": True}).status_code == 404
    assert c.post(f"/api/photos/{pid}/description", json={"description": "x"}).status_code == 404
    c.post("/api/locked/open", json={"pin": "2468"})
    assert c.post(f"/api/photos/{pid}/describe").json()["caption"] == "a private caption"
    conn.close()


# 5 ---------------------------------------------------------------- a long job swallowing an index request

def test_a_running_backup_or_movie_does_not_swallow_indexing(ctx, monkeypatch):
    from photointel.pipeline import jobs

    spawned = []
    monkeypatch.setattr(jobs, "_spawn", lambda args: spawned.append(args))
    conn = ctx.connect()
    for kind in ("backup", "create"):
        jid = jobs.create_job(conn, kind, {})
        conn.execute("UPDATE jobs SET status='running', heartbeat_at=? WHERE id=?", (time.time(), jid))
        conn.commit()
        new = jobs.spawn_index_job(ctx, {"kind": "index"})
        assert new != jid, kind
        conn.execute("UPDATE jobs SET status='done' WHERE id IN (?, ?)", (jid, new))
        conn.commit()
    assert len(spawned) == 2
    conn.close()


# 6 ---------------------------------------------------------------- a failing backup retried every minute

def test_a_failed_or_cancelled_backup_is_not_retried_every_minute():
    from types import SimpleNamespace

    from photointel.scheduler import due

    s = SimpleNamespace(auto_index_minutes=0, backup_folder="E:/bk", backup_every_days=7)
    now = 1_000_000.0
    assert due(s, now, now, None, False, False, last_backup_attempt=now - 120) == []
    assert due(s, now, now, None, False, False, last_backup_attempt=now - 7 * 3600) == ["backup"]
    assert due(s, now, now, None, False, False) == ["backup"]


# 7 ---------------------------------------------------------------- a stack losing its cover

def _stack(ctx, names):
    conn = ctx.connect()
    ids = [pid_of(ctx, n) for n in names]
    conn.execute(f"UPDATE photos SET stack_id = ?, stack_hidden = 1 WHERE id IN ({','.join('?' * len(ids))})",
                 (ids[0], *ids))
    conn.execute("UPDATE photos SET stack_hidden = 0 WHERE id = ?", (ids[0],))
    conn.commit()
    conn.close()
    return ids


@pytest.mark.parametrize("how", ["trash", "archive", "hide", "lock"])
def test_the_rest_of_a_stack_stays_in_the_timeline_when_its_cover_goes(ctx, library, app, how):
    index(ctx, library)
    c = TestClient(app)
    ids = _stack(ctx, ["IMG_x0.jpg", "IMG_x1.jpg", "IMG_x2.jpg"])
    if how == "trash":
        c.post("/api/trash", json={"photo_ids": [ids[0]], "confirm": 1})
    elif how == "archive":
        c.post("/api/photos/archive", json={"photo_ids": [ids[0]]})
    elif how == "hide":
        c.post("/api/photos/hide", json={"photo_ids": [ids[0]]})
    else:
        c.post("/api/locked/pin", json={"new": "2468"})
        c.post("/api/photos/lock", json={"photo_ids": [ids[0]]})
    shown = set(c.get("/api/photos/index", params={"collapse_stacks": True, "archived": "exclude"}).json()["ids"])
    assert len(shown & set(ids[1:])) == 1, how            # one of the other frames is the stack's new cover


# 8 ---------------------------------------------------------------- the cover you chose, undone by a rebuild

def test_a_chosen_stack_cover_survives_a_rebuild(ctx, tmp_path, app):
    from tests.test_v3_features import _burst_library

    root = tmp_path / "burst"
    _burst_library(root)
    index(ctx, root)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["quality", "stacks"])
    burst = [r[0] for r in conn.execute("SELECT id FROM photos WHERE filename LIKE 'IMG__.jpg' ORDER BY id")]
    cover = conn.execute("SELECT stack_id FROM photos WHERE id = ?", (burst[0],)).fetchone()[0]
    chosen = next(b for b in burst if b != cover)
    TestClient(app).post(f"/api/stacks/{cover}/cover", json={"photo_id": chosen})
    run_post_stages(ctx, conn, stages=["stacks"])
    assert conn.execute("SELECT stack_id FROM photos WHERE id = ?", (burst[0],)).fetchone()[0] == chosen
    conn.close()


# 9 ---------------------------------------------------------------- concurrent rotated thumbnails

def test_many_requests_for_one_rotated_thumbnail_at_once(ctx, library, app):
    index(ctx, library)
    c = TestClient(app)
    pid = pid_of(ctx, "IMG_x0.jpg")
    c.post("/api/photos/rotate", json={"photo_ids": [pid], "degrees": 90})
    barrier = threading.Barrier(8)
    codes = []

    def go():
        cl = TestClient(app)
        barrier.wait()
        codes.append(cl.get(f"/api/thumb/{pid}", params={"s": "l"}).status_code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [200] * 8
    assert not list((ctx.paths.thumbs / "rotated").glob("*.tmp"))


# 10 --------------------------------------------------------------- albums over 900 photos

def test_album_dates_and_cover_use_every_photo(ctx, library):
    from photointel.engine import albums

    index(ctx, library)
    conn = ctx.connect()
    root = conn.execute("SELECT id FROM roots").fetchone()[0]
    t0 = datetime(2010, 1, 1).timestamp()
    ids = []
    for i in range(1000):
        cur = conn.execute(
            "INSERT INTO photos(root_id, rel_path, folder, filename, ext, size, mtime, status, first_seen_at, last_seen_at, "
            "taken_ts, quality_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (root, f"synthetic/{i}.jpg", "synthetic", f"{i}.jpg", ".jpg", 1, 0, "ok", 0, 0,
             t0 + i * 86400, 0.9 if i == 999 else 0.1))
        ids.append(cur.lastrowid)
    aid = albums.create_album(conn, "Big", ids)
    a = next(x for x in albums.list_albums(conn) if x["id"] == aid)
    assert a["end_ts"] == pytest.approx(t0 + 999 * 86400)
    assert a["cover_photo_id"] == ids[999]
    conn.close()


# 11 --------------------------------------------------------------- second pass

def _family(app):
    owner = TestClient(app)
    owner.post("/api/auth/password", json={"new": "owner-pass"})
    owner.post("/api/accounts/enable", json={"username": "Sanjay"})
    owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    owner.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"})
    fam, guest = TestClient(app), TestClient(app)
    fam.post("/api/auth/login", json={"username": "Priya", "password": "priya-pass"})
    guest.post("/api/auth/login", json={"username": "Guest", "password": "guest-pass"})
    return owner, fam, guest


def test_only_the_owner_publishes_share_links(ctx, library, app):
    index(ctx, library)
    owner, fam, guest = _family(app)
    album = owner.post("/api/albums", json={"name": "Goa", "photo_ids": [pid_of(ctx, "IMG_x0.jpg")]}).json()["id"]
    token = owner.post(f"/api/albums/{album}/share", json={"allow_upload": True}).json()["token"]
    assert fam.post(f"/api/albums/{album}/share", json={"allow_upload": True}).status_code == 403
    assert guest.get(f"/api/albums/{album}/shares").status_code == 403
    assert fam.get(f"/api/albums/{album}/shares").status_code == 403
    assert fam.delete(f"/api/shares/{token}").status_code == 403
    assert owner.get(f"/api/albums/{album}/shares").json()["shares"][0]["token"] == token


def test_settings_do_not_show_server_paths_to_others(ctx, library, app):
    index(ctx, library)
    owner, fam, guest = _family(app)
    def strings(o):          # every string value in a JSON body (str(dict) would escape Windows paths)
        if isinstance(o, dict):
            return [x for v in o.values() for x in strings(v)]
        if isinstance(o, list):
            return [x for v in o for x in strings(v)]
        return [o] if isinstance(o, str) else []

    for c in (fam, guest):
        body = c.get("/api/settings").json()
        found = strings(body)
        assert not any(str(library) in x or str(ctx.paths.data) in x for x in found), found
        assert "allow_online_map_tiles" in body["settings"]
    assert any(str(library) in x for x in strings(owner.get("/api/settings").json()))


def test_an_event_cover_that_left_view_is_replaced(ctx, library, app):
    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["people", "events"])
    c = TestClient(app)
    ev = next(e for e in c.get("/api/events").json()["events"] if e["photo_count"] >= 5)
    cover = ev["cover_photo_id"]
    c.post("/api/trash", json={"photo_ids": [cover], "confirm": 1})
    for got in (next(e for e in c.get("/api/events").json()["events"] if e["id"] == ev["id"])["cover_photo_id"],
                c.get(f"/api/events/{ev['id']}").json()["cover_photo_id"]):
        assert got is not None and got != cover
        assert conn.execute("SELECT status FROM photos WHERE id = ?", (got,)).fetchone()[0] == "ok"
    conn.close()
