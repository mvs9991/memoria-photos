"""v7: the WebDAV drop box for phone backup apps, XMP sidecar import, private favourites and
albums per account, and offline caching rules."""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photointel.pipeline.indexer import Indexer


def index(ctx, root) -> None:
    Indexer(ctx, workers=2).run(roots=[str(root)])


def _jpeg(colour=(10, 120, 200), taken: str | None = "2023:08:15 10:30:00") -> bytes:
    import piexif

    img = Image.new("RGB", (320, 240), colour)
    buf = io.BytesIO()
    kw = {}
    if taken:
        kw["exif"] = piexif.dump({"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: taken.encode()}, "GPS": {}})
    img.save(buf, "JPEG", **kw)
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
    return owner


PRIYA = ("Priya", "priya-pass")


def propfind(c, path, depth="1", auth=PRIYA):
    return c.request("PROPFIND", path, headers={"Depth": depth}, auth=auth)


# ----------------------------------------------------------------- WebDAV

def test_a_backup_app_uploads_lists_and_never_deletes(ctx, library, app, family):
    index(ctx, library)
    dav = TestClient(app)
    assert dav.request("PROPFIND", "/dav/").status_code == 401
    assert "Basic" in dav.request("PROPFIND", "/dav/").headers["www-authenticate"]
    assert propfind(dav, "/dav/", auth=("Priya", "nope")).status_code == 401
    assert dav.options("/dav/").headers["dav"] == "1"

    assert dav.request("MKCOL", "/dav/DCIM", auth=PRIYA).status_code == 201
    assert dav.request("MKCOL", "/dav/DCIM/Camera", auth=PRIYA).status_code == 201
    photo = _jpeg()
    assert dav.put("/dav/DCIM/Camera/IMG_1.jpg", content=photo, auth=PRIYA).status_code == 201
    stored = ctx.paths.data / "uploads" / "Priya" / "2023" / "08" / "IMG_1.jpg"
    assert stored.read_bytes() == photo

    listing = propfind(dav, "/dav/DCIM/Camera").text
    assert "/dav/DCIM/Camera/IMG_1.jpg" in listing and f"<D:getcontentlength>{len(photo)}<" in listing
    assert "/dav/DCIM/Camera/" in propfind(dav, "/dav/DCIM").text
    assert dav.get("/dav/DCIM/Camera/IMG_1.jpg", auth=PRIYA).content == photo

    # the app sends it again (its own bookkeeping was lost): same bytes, nothing new stored
    assert dav.put("/dav/DCIM/Camera/IMG_1.jpg", content=photo, auth=PRIYA).status_code == 204
    assert len(list(stored.parent.iterdir())) == 1
    # a photo already in the library is listed, not stored twice
    lib_bytes = (library / "Trips/Goa/IMG_x0.jpg").read_bytes()
    assert dav.put("/dav/DCIM/Camera/IMG_x0.jpg", content=lib_bytes, auth=PRIYA).status_code == 201
    assert "IMG_x0.jpg" in propfind(dav, "/dav/DCIM/Camera").text
    assert not (ctx.paths.data / "uploads" / "Priya" / "2024").exists()

    # upload under a temporary name, then MOVE into place
    assert dav.put("/dav/DCIM/Camera/IMG_2.jpg.part", content=_jpeg((200, 30, 30)), auth=PRIYA).status_code == 201
    r = dav.request("MOVE", "/dav/DCIM/Camera/IMG_2.jpg.part", auth=PRIYA,
                    headers={"Destination": "http://testserver/dav/DCIM/Camera/IMG_2.jpg"})
    assert r.status_code == 201 and (stored.parent / "IMG_2.jpg").exists()
    assert not list((ctx.paths.data / "uploads" / ".incoming").iterdir())

    # a phone deleting a photo never deletes the backup
    assert dav.delete("/dav/DCIM/Camera/IMG_1.jpg", auth=PRIYA).status_code == 403
    assert stored.exists()
    conn = ctx.connect()
    from photointel import db
    assert db.get_meta(conn, "uploads_pending_since")                 # indexed after a pause
    conn.close()


def test_webdav_roles_paths_and_guessing(ctx, library, app, family):
    dav = TestClient(app)
    guest = ("Guest", "guest-pass")
    assert propfind(dav, "/dav/", auth=guest).status_code == 207          # a guest may look
    assert dav.put("/dav/a.jpg", content=_jpeg(), auth=guest).status_code == 403
    assert dav.put("/dav/%2e%2e/escape.jpg", content=_jpeg(), auth=PRIYA).status_code in (400, 404)
    assert not (ctx.paths.data / "escape.jpg").exists()
    # each account sees only what it uploaded
    dav.put("/dav/p.jpg", content=_jpeg((1, 2, 3)), auth=PRIYA)
    assert "p.jpg" not in propfind(dav, "/dav/", auth=("Sanjay", "owner-pass")).text
    for i in range(10):
        dav.request("PROPFIND", "/dav/", auth=("Priya", f"guess{i}"), headers={"Depth": "0"})
    assert propfind(dav, "/dav/", depth="0").status_code == 429


def test_webdav_needs_a_password_beyond_this_machine(ctx, app):
    assert TestClient(app).request("PROPFIND", "/dav/", headers={"Depth": "0"}).status_code == 403


def test_uploads_are_indexed_once_they_pause():
    from photointel.scheduler import uploads_due

    now = 1000.0
    assert not uploads_due(None, now, False)
    assert not uploads_due(now - 30, now, False)
    assert uploads_due(now - 61, now, False)
    assert not uploads_due(now - 61, now, True)
