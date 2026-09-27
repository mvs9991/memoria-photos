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


# ----------------------------------------------------------------- XMP sidecars from other apps

DIGIKAM = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:lr="http://ns.adobe.com/lightroom/1.0/" xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"
    xmp:Rating="4">
   <dc:subject><rdf:Bag><rdf:li>{kw}</rdf:li><rdf:li>Priya</rdf:li></rdf:Bag></dc:subject>
   <lr:hierarchicalSubject><rdf:Bag><rdf:li>People|Priya</rdf:li></rdf:Bag></lr:hierarchicalSubject>
   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{desc}</rdf:li></rdf:Alt></dc:description>
   <mwg-rs:Regions rdf:parseType="Resource"><mwg-rs:RegionList><rdf:Bag>
     <rdf:li rdf:parseType="Resource"><mwg-rs:Name>Priya</mwg-rs:Name><mwg-rs:Type>Face</mwg-rs:Type></rdf:li>
   </rdf:Bag></mwg-rs:RegionList></mwg-rs:Regions>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

LIGHTROOM = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/"><xmp:Rating>5</xmp:Rating></rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""


def test_parse_sidecars_from_other_apps():
    from photointel.engine.xmp_import import parse_sidecar

    d = parse_sidecar(DIGIKAM.format(kw="Beach", desc="Sunset at Baga").encode())
    assert d == {"rating": 4, "keywords": ["Beach"], "description": "Sunset at Baga", "people": ["Priya"],
                 "tool": "XMP Core 4.4.0-Exiv2"}
    assert parse_sidecar(LIGHTROOM.encode())["rating"] == 5
    assert parse_sidecar(b"not xml") is None


def test_sidecars_bring_stars_keywords_and_name_suggestions(ctx, library, app):
    from photointel.engine.takeout import name_suggestions
    from photointel.engine.xmp_import import import_xmp
    from photointel.pipeline.post import run_post_stages

    goa = library / "Trips/Goa"
    for i in range(4):
        (goa / f"IMG_x{i}.jpg.xmp").write_text(DIGIKAM.format(kw="Beach", desc=f"Day {i}"), encoding="utf-8")
    (goa / "IMG_x4.xmp").write_text(LIGHTROOM, encoding="utf-8")                   # Lightroom names it by stem
    index(ctx, library)
    conn = ctx.connect()
    conn.execute("UPDATE photos SET rating = 2 WHERE filename = 'IMG_x3.jpg'")      # set here already: kept
    conn.commit()
    run_post_stages(ctx, conn, stages=["people"])
    out = import_xmp(ctx, conn)
    assert out["sidecars"] == 5
    row = lambda n: conn.execute("SELECT * FROM photos WHERE filename = ?", (n,)).fetchone()  # noqa: E731
    assert row("IMG_x0.jpg")["rating"] == 4 and row("IMG_x0.jpg")["description"] == "Day 0"
    assert row("IMG_x3.jpg")["rating"] == 2 and row("IMG_x4.jpg")["rating"] == 5
    tagged = {r[0] for r in conn.execute(
        "SELECT p.filename FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id JOIN photos p ON p.id = pt.photo_id "
        "WHERE t.name = 'beach' AND pt.source = 'user'")}
    assert tagged == {f"IMG_x{i}.jpg" for i in range(4)}
    assert not conn.execute("SELECT 1 FROM tags WHERE name = 'priya'").fetchone()     # a person, not a keyword
    sug = name_suggestions(conn)
    assert [s["name"] for s in sug] == ["Priya"]                                         # suggested, not applied
    assert conn.execute("SELECT COUNT(*) FROM persons WHERE name = 'Priya'").fetchone()[0] == 0

    # removing a tag here sticks; a keyword added later in the other app arrives
    pid = row("IMG_x0.jpg")["id"]
    TestClient(app).post("/api/photos/tags/remove", json={"photo_ids": [pid], "name": "beach"})
    side = goa / "IMG_x0.jpg.xmp"
    side.write_text(DIGIKAM.format(kw="Beach</rdf:li><rdf:li>Sunset", desc="Day 0"), encoding="utf-8")
    later = time.time() + 5
    import os
    os.utime(side, (later, later))
    import_xmp(ctx, conn)
    names = {r[0] for r in conn.execute("SELECT t.name FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
                                        "WHERE pt.photo_id = ? AND pt.source = 'user'", (pid,))}
    assert names == {"sunset"}
    conn.close()


def test_memoria_own_exports_are_not_read_back(ctx, library):
    from photointel.engine.xmp_import import import_xmp

    (library / "Trips/Goa/IMG_x0.jpg.xmp").write_text(
        DIGIKAM.replace("XMP Core 4.4.0-Exiv2", "Memoria").format(kw="auto-tag", desc="x"), encoding="utf-8")
    index(ctx, library)
    conn = ctx.connect()
    out = import_xmp(ctx, conn)
    assert out.get("memoria_exports_skipped") == 1 and not out.get("sidecars")
    conn.close()


def test_a_stem_sidecar_goes_to_the_raw_file(ctx, tmp_path):
    from datetime import datetime

    from photointel.engine.xmp_import import import_xmp
    from tests.conftest import make_image

    root = tmp_path / "cam"
    make_image(root / "DSC_1.JPG", taken=datetime(2024, 6, 1, 8, 0))
    make_image(root / "DSC_1.tmp.jpg", colour=(10, 200, 40), taken=datetime(2024, 6, 1, 8, 0, 30))
    (root / "DSC_1.xmp").write_text(LIGHTROOM, encoding="utf-8")
    index(ctx, root)
    conn = ctx.connect()
    conn.execute("UPDATE photos SET filename='DSC_1.NEF', ext='.nef' WHERE filename='DSC_1.tmp.jpg'")  # stand-in RAW
    conn.commit()
    import_xmp(ctx, conn)
    ratings = dict(conn.execute("SELECT filename, rating FROM photos").fetchall())
    assert ratings == {"DSC_1.JPG": 0, "DSC_1.NEF": 5}
    conn.close()
