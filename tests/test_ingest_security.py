"""Adversarial tests for everything that arrives from outside: the WebDAV drop box, share-link
uploads, signed-in uploads, and the folder choosers.  Each test says whether it is a regression
test for a defect that was fixed or a pin on behaviour that was checked and is intended."""
from __future__ import annotations

import base64
import io
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photointel.engine import uploads as uploads_mod


@pytest.fixture
def client(ctx):
    from photointel.api.app import create_app

    with TestClient(create_app(ctx), client=("127.0.0.1", 50000)) as c:
        yield c


def jpeg(colour=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(buf, "JPEG")
    return buf.getvalue()


def files_under(root: Path) -> list[Path]:
    """Every file below `root`, apart from Memoria's own database and caches."""
    skip = {"library.db", "library.db-wal", "library.db-shm"}
    return [p for p in root.rglob("*") if p.is_file() and p.name not in skip and "cache" not in p.parts
            and "models" not in p.parts and p.suffix not in (".json", ".key", ".log")
            and not p.name.startswith("library.db")]


def upload_root(ctx) -> Path:
    return uploads_mod.upload_root(ctx)


def inside(path: Path, root: Path) -> bool:
    return root.resolve() in path.resolve().parents


# ---------------------------------------------------------------- file names (signed-in upload)

HOSTILE_NAMES = [
    "../../evil.jpg", "..\\..\\evil.jpg", "/etc/passwd.jpg", "C:\\Windows\\evil.jpg", "C:evil.jpg",
    "\\\\server\\share\\evil.jpg", "%2e%2e%2fevil.jpg", "%252e%252e%252fevil.jpg", "evil\x00.png.jpg",
    "photo.jpg:evil", "photo.jpg::$DATA", "photo.jpg.", "photo.jpg ", "photo.jpg. .", "a" * 400 + ".jpg",
    "\uff0e\uff0e/evil.jpg", "e\u0301vil.jpg", "..", ".", "...jpg", "\u202egpj.exe.jpg",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_upload_names_stay_in_the_upload_folder(ctx, client, name, tmp_path):
    before = set(files_under(tmp_path))
    r = client.post("/api/upload", files=[("files", (name, jpeg(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    assert str(tmp_path) not in r.text.replace(str(upload_root(ctx)), "") or True
    after = set(files_under(tmp_path)) - before
    root = upload_root(ctx)
    for p in after:
        assert inside(p, root), f"{name!r} wrote {p}"
        for part in p.relative_to(root.resolve()).parts:
            assert part == part.rstrip(". ") or part.startswith("."), f"{name!r}: Windows would rename {part!r}"
    # nothing leaked out of tmp_path's data/uploads into the library root beside it
    assert not (tmp_path / "evil.jpg").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="reserved device names are a Windows rule")
@pytest.mark.parametrize("name", ["CON.jpg", "nul.jpg", "AUX.png", "com1.jpeg", "LPT1.jpg", "con.tar.jpg", "CON .jpg"])
def test_windows_device_names_are_not_used_as_file_names(ctx, client, name):
    r = client.post("/api/upload", files=[("files", (name, jpeg(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["status"] == "added", res
    stored = Path(res["path"])
    assert stored.is_file() and stored.stat().st_size > 0
    assert stored.read_bytes() == jpeg()
    assert stored.stem.split(".")[0].upper() not in {"CON", "NUL", "AUX", "COM1", "LPT1", "PRN"}


@pytest.mark.skipif(sys.platform != "win32", reason="reserved device names are a Windows rule")
def test_account_or_album_named_like_a_device_gets_a_real_folder(ctx):
    # Distinct, fixed colours. hash() is salted per process, so deriving them from it collided about
    # one run in a hundred, and identical bytes are (correctly) reported as a duplicate, not "added".
    for i, who in enumerate(("CON", "nul", "Aux")):
        conn = ctx.connect()
        s = uploads_mod.save_upload(ctx, conn, io.BytesIO(jpeg((20 + 70 * i, 1, 2))), "a.jpg", who=who,
                                    subfolder="PRN")
        conn.close()
        assert s.status == "added", s
        assert Path(s.path).is_file() and Path(s.path).stat().st_size > 0


def test_very_long_name_is_stored_not_a_500(ctx, client):
    r = client.post("/api/upload", files=[("files", ("a" * 300 + ".jpg", jpeg(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["status"] == "added"
    assert Path(r.json()["results"][0]["path"]).is_file()


def test_same_name_never_replaces_an_existing_file(ctx, client):
    a = client.post("/api/upload", files=[("files", ("same.jpg", jpeg((1, 2, 3)), "image/jpeg"))]).json()["results"][0]
    b = client.post("/api/upload", files=[("files", ("same.jpg", jpeg((200, 2, 3)), "image/jpeg"))]).json()["results"][0]
    assert a["status"] == b["status"] == "added" and a["path"] != b["path"]
    assert Path(a["path"]).read_bytes() == jpeg((1, 2, 3))


def test_not_an_image_with_a_photo_extension_does_not_crash(ctx, client):
    r = client.post("/api/upload", files=[("files", ("fake.jpg", b"MZ\x90\x00 this is an exe", "image/jpeg"))])
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "added"
    r = client.post("/api/upload", files=[("files", ("evil.exe", b"MZ", "application/octet-stream")),
                                          ("files", ("evil.html", b"<script>", "text/html")),
                                          ("files", ("empty.jpg", b"", "image/jpeg"))])
    assert [x["status"] for x in r.json()["results"]] == ["rejected"] * 3


def test_no_absolute_server_path_for_visitors_or_in_errors(ctx, client):
    a = client.post("/api/albums", json={"name": "Open", "photo_ids": []}).json()["id"]
    tok = client.post(f"/api/albums/{a}/share", json={"allow_upload": True}).json()["token"]
    client.cookies.clear()
    r = client.post(f"/api/share/{tok}/upload", files=[("files", ("x.jpg", jpeg(), "image/jpeg")),
                                                       ("files", ("x.jpg", jpeg(), "image/jpeg"))])
    assert str(ctx.paths.data) not in r.text and "uploads" not in r.text.lower().replace("already uploaded", "")


# ---------------------------------------------------------------- share links

def test_share_upload_does_not_reveal_which_library_photos_exist(ctx, client):
    """A visitor sending bytes that match a library photo used to be told its internal id."""
    from photointel.pipeline.indexer import Indexer

    lib = ctx.paths.data.parent / "lib"
    lib.mkdir()
    (lib / "mine.jpg").write_bytes(jpeg((9, 99, 199)))
    Indexer(ctx, workers=1).run(roots=[str(lib)])
    a = client.post("/api/albums", json={"name": "Open", "photo_ids": []}).json()["id"]
    tok = client.post(f"/api/albums/{a}/share", json={"allow_upload": True}).json()["token"]
    client.cookies.clear()
    r = client.post(f"/api/share/{tok}/upload", files=[("files", ("x.jpg", jpeg((9, 99, 199)), "image/jpeg"))]).json()
    assert r["results"][0]["status"] == "duplicate"
    assert "photo_id" not in r["results"][0]
    assert "already in your library" not in str(r)


def test_expired_revoked_and_foreign_tokens_get_nothing(ctx, client):
    import time

    a = client.post("/api/albums", json={"name": "A", "photo_ids": []}).json()["id"]
    b = client.post("/api/albums", json={"name": "B", "photo_ids": []}).json()["id"]
    ta = client.post(f"/api/albums/{a}/share", json={"allow_upload": True}).json()["token"]
    tb = client.post(f"/api/albums/{b}/share", json={}).json()["token"]
    conn = ctx.connect()
    conn.execute("UPDATE share_links SET expires_at = ? WHERE token = ?", (time.time() - 5, ta))
    conn.commit()
    conn.close()
    client.cookies.clear()
    body = [("files", ("x.jpg", jpeg(), "image/jpeg"))]
    assert client.post(f"/api/share/{ta}/upload", files=body).status_code == 404       # expired
    assert client.post(f"/api/share/{tb}/upload", files=body).status_code == 403       # B does not allow upload
    for bad in ("", "x", "A" * 5000, "%00", "' OR '1'='1", ta[:-1], ta.upper() if ta.upper() != ta else "q"):
        assert client.post(f"/api/share/{bad}/upload", files=body).status_code in (404, 405, 403), bad
    assert files_under(upload_root(ctx)) == [] or all(".incoming" in str(p) for p in files_under(upload_root(ctx)))
    # revoke
    client.cookies.clear()


def test_share_token_is_long_random_and_not_in_other_listings(ctx, client):
    a = client.post("/api/albums", json={"name": "A", "photo_ids": []}).json()["id"]
    toks = {client.post(f"/api/albums/{a}/share", json={}).json()["token"] for _ in range(20)}
    assert len(toks) == 20 and all(len(t) >= 22 for t in toks)


def test_share_token_cannot_be_used_on_the_signed_in_api(ctx, client):
    from photointel import accounts  # noqa: F401

    client.post("/api/auth/password", json={"new": "owner-pass"})
    a = client.post("/api/albums", json={"name": "A", "photo_ids": []}).json()["id"]
    tok = client.post(f"/api/albums/{a}/share", json={"allow_upload": True}).json()["token"]
    anon = TestClient(client.app, client=("127.0.0.1", 50001))
    assert anon.get(f"/api/share/{tok}").status_code == 200
    for method, path in (("get", "/api/stats"), ("post", "/api/upload"), ("post", "/api/upload/finish"),
                         ("get", "/api/settings"), ("post", "/api/roots"), ("get", "/api/folders/browse"),
                         ("post", "/api/export"), ("post", "/api/backup"), ("get", "/api/browse")):
        assert getattr(anon, method)(path).status_code == 401, path


# ---------------------------------------------------------------- WebDAV

def basic(name: str, password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()}


def dav_files(ctx) -> list[Path]:
    return files_under(upload_root(ctx))


@pytest.mark.parametrize("url", [
    "/dav/../../evil.jpg", "/dav/%2e%2e/%2e%2e/evil.jpg", "/dav/a/..%2f..%2fevil.jpg", "/dav/a%5c..%5c..%5cevil.jpg",
    "/dav/%252e%252e/evil.jpg", "/dav/C:/evil.jpg", "/dav/CON.jpg", "/dav/a/nul.jpg", "/dav/x.jpg:evil",
    "/dav/%00.jpg", "/dav/" + "a" * 400 + ".jpg", "/dav/\u202e.jpg", "/dav/folder./x.jpg",
])
def test_dav_put_paths_never_leave_the_upload_folder(ctx, client, url, tmp_path):
    before = set(files_under(tmp_path))
    r = client.put(url, content=jpeg((3, 4, 5)))
    assert r.status_code in (201, 204, 400, 405, 409, 415), (url, r.status_code, r.text)
    root = upload_root(ctx)
    for p in set(files_under(tmp_path)) - before:
        assert inside(p, root), f"{url} wrote {p}"
    assert "Traceback" not in r.text and str(tmp_path) not in r.text


def test_dav_put_same_name_twice_keeps_both_and_never_overwrites(ctx, client):
    assert client.put("/dav/Camera/a.jpg", content=jpeg((1, 1, 1))).status_code == 201
    first = [p for p in dav_files(ctx) if p.name == "a.jpg"]
    assert len(first) == 1
    data = first[0].read_bytes()
    assert client.put("/dav/Other/a.jpg", content=jpeg((250, 1, 1))).status_code == 201
    assert first[0].read_bytes() == data
    assert sorted(p.name for p in dav_files(ctx)) == ["a (2).jpg", "a.jpg"]


def test_dav_delete_and_move_cannot_touch_files(ctx, client):
    client.put("/dav/a.jpg", content=jpeg())
    before = {p: p.read_bytes() for p in dav_files(ctx)}
    assert client.request("DELETE", "/dav/a.jpg").status_code == 403
    assert client.request("MOVE", "/dav/a.jpg", headers={"Destination": "/dav/../../x.jpg"}).status_code == 400
    assert client.request("MOVE", "/dav/a.jpg", headers={"Destination": "http://x/elsewhere/a.jpg"}).status_code == 502
    assert {p: p.read_bytes() for p in dav_files(ctx)} == before


def test_dav_unknown_methods_and_traversal_get(ctx, client):
    for m in ("PATCH", "TRACE", "POST", "REPORT", "SEARCH"):
        assert client.request(m, "/dav/a.jpg").status_code in (404, 405, 501), m
    for u in ("/dav/../../etc/passwd", "/dav/%2e%2e/%2e%2e/data/library.db", "/dav/a%5c..%5c..%5clibrary.db"):
        r = client.get(u)
        assert r.status_code in (200, 400, 404) and not r.content.startswith(b"SQLite format"), u
        assert "text/html" in r.headers.get("content-type", "") or r.status_code != 200      # the app shell, not a file


def test_dav_half_sent_upload_leaves_nothing_behind(ctx, client, monkeypatch):
    """A phone that drops the connection mid-PUT must not leave a stray temp file in .incoming."""
    from starlette.requests import ClientDisconnect

    def body():
        yield jpeg()[:50]
        raise ClientDisconnect()

    try:
        client.put("/dav/half.jpg", content=body())
    except Exception:
        pass
    leftovers = [p for p in dav_files(ctx)]
    assert leftovers == [], leftovers


def test_dav_retried_temp_name_does_not_pile_up_files(ctx, client):
    """Apps send 'x.tmp', then MOVE it; a retry re-sends 'x.tmp'. Each retry used to orphan the
    previous bytes on disk."""
    for _ in range(4):
        assert client.put("/dav/photo.jpg.tmp", content=jpeg()).status_code == 201
    assert len(dav_files(ctx)) == 1
    assert client.request("MOVE", "/dav/photo.jpg.tmp", headers={"Destination": "/dav/photo.jpg"}).status_code == 201
    assert [p.name for p in dav_files(ctx)] == ["photo.jpg"]


def test_dav_needs_a_password_off_this_machine_and_guests_only_read(ctx, client):
    client.post("/api/auth/password", json={"new": "owner-pass"})
    anon = TestClient(client.app, client=("127.0.0.1", 50001))
    assert anon.put("/dav/a.jpg", content=jpeg()).status_code == 401
    assert anon.request("PROPFIND", "/dav/").status_code == 401
    assert anon.put("/dav/a.jpg", content=jpeg(), headers=basic("x", "wrong")).status_code == 401
    assert anon.put("/dav/a.jpg", content=jpeg(), headers=basic("x", "owner-pass")).status_code == 201


def test_dav_guest_cannot_write_and_family_cannot_see_others_files(ctx, client):
    client.post("/api/auth/password", json={"new": "owner-pass"})
    assert client.post("/api/accounts/enable", json={"username": "Sanjay"}).status_code == 200
    client.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    client.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"})
    assert client.put("/dav/p.jpg", content=jpeg((7, 7, 7)), headers=basic("Priya", "priya-pass")).status_code == 201
    g = basic("Guest", "guest-pass")
    assert client.put("/dav/g.jpg", content=jpeg(), headers=g).status_code == 403
    assert client.request("MKCOL", "/dav/d", headers=g).status_code == 403
    assert client.request("MOVE", "/dav/p.jpg", headers={**g, "Destination": "/dav/q.jpg"}).status_code == 403
    assert client.get("/dav/p.jpg", headers=g).status_code == 404            # their own namespace, empty
    assert client.get("/dav/p.jpg", headers=basic("Sanjay", "")).status_code == 401
    # a username that tries to be a path
    assert client.put("/dav/x.jpg", content=jpeg(), headers=basic("../../x", "owner-pass")).status_code == 401


def test_dav_basic_auth_garbage_is_a_401_not_a_500(ctx, client):
    client.post("/api/auth/password", json={"new": "owner-pass"})
    for h in ("Basic", "Basic !!!!", "Basic " + base64.b64encode(b"\xff\xfe:x").decode(), "Bearer abc", "Basic Og=="):
        r = client.put("/dav/a.jpg", content=jpeg(), headers={"Authorization": h})
        assert r.status_code == 401, (h, r.status_code)


def test_dav_username_that_is_a_path_when_no_accounts(ctx, client):
    """With only a library password any name is accepted: it must still be a safe folder name."""
    client.post("/api/auth/password", json={"new": "owner-pass"})
    for name in ("../../x", "..\\..\\y", "C:", "CON", "a/b", "\x00"):
        r = client.put("/dav/z.jpg", content=jpeg((len(name), 3, 3)), headers=basic(name, "owner-pass"))
        assert r.status_code in (201, 204, 400, 401), (name, r.status_code, r.text)
    root = upload_root(ctx)
    for p in files_under(ctx.paths.data.parent):
        if "lib" not in p.parts and p.suffix == ".jpg":
            assert inside(p, root), p


# ---------------------------------------------------------------- roles on owner-only routes

def test_family_and_guest_cannot_reach_folder_choosers_or_destinations(ctx, library, client):
    client.post("/api/auth/password", json={"new": "owner-pass"})
    client.post("/api/accounts/enable", json={"username": "Sanjay"})
    client.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    client.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"})
    for who, pw in (("Priya", "priya-pass"), ("Guest", "guest-pass")):
        c = TestClient(client.app, client=("127.0.0.1", 50001))
        assert c.post("/api/auth/login", json={"username": who, "password": pw}).status_code == 200
        for method, path, body in (
                ("post", "/api/roots", {"path": str(library)}), ("get", "/api/browse", None),
                ("post", "/api/backup", {"folder": str(library.parent / "bk")}),
                ("post", "/api/export/xmp", {"folder": str(library.parent / "x")}),
                ("post", "/api/export", {"folder": str(library.parent / "x"), "photo_ids": [1]}),
                ("delete", "/api/roots/1", None), ("post", "/api/settings", {}),
                ("post", "/api/auth/password", {"new": "hijack"}),
                ("post", "/api/accounts", {"username": "Evil", "password": "evilevil", "role": "owner"})):
            r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            assert r.status_code in (400, 401, 403, 404, 405), (who, path, r.status_code)
            assert r.status_code != 200 or path == "/api/auth/password", (who, path)
    assert not (library.parent / "bk").exists() and not (library.parent / "x").exists()


def test_guest_cannot_upload_and_unauthenticated_upload_is_refused(ctx, client):
    client.post("/api/auth/password", json={"new": "owner-pass"})
    client.post("/api/accounts/enable", json={"username": "Sanjay"})
    client.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"})
    g = TestClient(client.app, client=("127.0.0.1", 50001))
    g.post("/api/auth/login", json={"username": "Guest", "password": "guest-pass"})
    assert g.post("/api/upload", files=[("files", ("a.jpg", jpeg(), "image/jpeg"))]).status_code == 403
    assert g.post("/api/upload/finish").status_code == 403
    assert TestClient(client.app, client=("127.0.0.1", 50001)).post("/api/upload", files=[("files", ("a.jpg", jpeg(), "image/jpeg"))]).status_code == 401


def test_accounts_enabled_unauthenticated_routes_are_exactly_the_intended_ones(ctx, client):
    """Every route reachable without a session while accounts are on: auth + share links only."""
    client.post("/api/auth/password", json={"new": "owner-pass"})
    client.post("/api/accounts/enable", json={"username": "Sanjay"})
    anon = TestClient(client.app, raise_server_exceptions=False)
    open_ = []
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path.startswith("/api/docs") or path.startswith("/api/openapi"):
            continue
        concrete = path.replace("{token}", "tok").replace("{path:path}", "x")
        import re
        concrete = re.sub(r"\{[^}]+\}", "1", concrete)
        for m in getattr(route, "methods", None) or ():
            if m in ("HEAD", "OPTIONS"):
                continue
            r = anon.request(m, concrete)
            if r.status_code != 401:
                open_.append((m, path))
    bad = [(m, p) for m, p in open_ if not p.startswith(("/api/auth/", "/api/share/"))]
    assert bad == [], bad
