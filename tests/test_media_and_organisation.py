"""Video, Live/motion photos, albums, Google Takeout, OCR text and user tags."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photointel import db, video
from photointel.engine import albums as albums_mod
from photointel.engine import people as people_mod
from photointel.engine import takeout as takeout_mod
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from tests.conftest import make_image

av = pytest.importorskip("av")


# ----------------------------------------------------------------- builders

def make_video(path: Path, seconds: float = 2.0, colour=(100, 150, 200), codec: str = "libx264",
               size=(320, 240), rotation: int = 0, metadata: dict | None = None, fps: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    options = {"movflags": "use_metadata_tags"} if path.suffix.lower() in (".mp4", ".mov", ".m4v") else {}
    out = av.open(str(path), "w", options=options)
    for k, v in (metadata or {}).items():
        out.metadata[k] = v
    s = out.add_stream(codec, rate=fps)
    s.width, s.height, s.pix_fmt = size[0], size[1], "yuv420p"
    if rotation:
        s.set_display_rotation(rotation)
    frame = np.full((size[1], size[0], 3), colour, np.uint8)
    frame[size[1] // 4: size[1] // 2, size[0] // 4: size[0] // 2] = (colour[2], colour[0], colour[1])
    for _ in range(int(seconds * fps)):
        for pkt in s.encode(av.VideoFrame.from_ndarray(frame, format="rgb24")):
            out.mux(pkt)
    for pkt in s.encode():
        out.mux(pkt)
    out.close()
    return path


def make_motion_photo(path: Path, colour=(90, 60, 200)) -> tuple[Path, int]:
    """A Google-style motion photo: JPEG with a MicroVideo XMP declaration + an MP4 appended."""
    jpg = make_image(path.with_suffix(".tmp.jpg"), colour=colour, taken=datetime(2024, 5, 1, 10, 0))
    data = jpg.read_bytes()
    jpg.unlink()
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
           b'GCamera:MicroVideo="1" GCamera:MicroVideoVersion="1"/></rdf:RDF></x:xmpmeta>')
    ns = b"http://ns.adobe.com/xap/1.0/\x00"
    seg = b"\xff\xe1" + (len(ns) + len(xmp) + 2).to_bytes(2, "big") + ns + xmp
    mp4 = make_video(path.parent / "_motion_part.mp4", seconds=1.5, colour=colour).read_bytes()
    (path.parent / "_motion_part.mp4").unlink()
    body = data[:2] + seg + data[2:]
    path.write_bytes(body + mp4)
    return path, len(body)


def index(ctx, root) -> None:
    Indexer(ctx, workers=2).run(roots=[str(root)])


def local_from_utc(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


@pytest.fixture
def client_for(ctx):
    from photointel.api.app import create_app

    clients = []

    def make():
        c = TestClient(create_app(ctx))
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


# ----------------------------------------------------------------- schema

def test_v1_database_migrates_to_v2_keeping_its_data(tmp_path):
    path = tmp_path / "library.db"
    conn = db.init_db(path)
    for name, _ in db.V2_PHOTO_COLUMNS:          # turn it back into a v1 photos table
        conn.execute(f"DROP INDEX IF EXISTS ix_photos_media")
        conn.execute(f"DROP INDEX IF EXISTS ix_photos_live")
        conn.execute(f"ALTER TABLE photos DROP COLUMN {name}")
    db.set_meta(conn, "schema_version", 1)
    conn.execute("INSERT INTO roots(path, added_at) VALUES ('x', 0)")
    conn.execute("INSERT INTO photos(root_id, rel_path, folder, filename, ext, size, mtime, first_seen_at, "
                 "last_seen_at, status) VALUES (1, 'a.jpg', '', 'a.jpg', '.jpg', 10, 0, 0, 0, 'ok')")
    conn.commit()
    conn.close()

    conn = db.init_db(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    assert {n for n, _ in db.V2_PHOTO_COLUMNS} <= cols
    row = conn.execute("SELECT filename, media_type, live_component FROM photos").fetchone()
    assert tuple(row) == ("a.jpg", "image", 0)
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    db.init_db(path).close()                       # and running it again is harmless


# ----------------------------------------------------------------- video

def test_video_is_indexed_with_its_own_metadata(ctx, tmp_path, client_for):
    root = tmp_path / "lib"
    make_video(root / "VID_clip.mp4", seconds=3, metadata={
        "com.apple.quicktime.creationdate": "2023-11-04T18:45:10+0530",
        "com.apple.quicktime.location.ISO6709": "+15.5439+073.7553+010.000/",
        "com.apple.quicktime.make": "Apple", "com.apple.quicktime.model": "iPhone 14",
    })
    index(ctx, root)
    conn = ctx.connect()
    r = conn.execute("SELECT * FROM photos WHERE filename='VID_clip.mp4'").fetchone()
    assert r["status"] == "ok" and r["media_type"] == "video"
    assert r["duration"] == pytest.approx(3.0, abs=0.2)
    assert r["taken_local"] == "2023-11-04 18:45:10"          # local wall clock, offset honoured
    assert r["date_source"] == "video_meta" and r["date_confidence"] == "high"
    assert (r["gps_lat"], r["gps_lon"]) == pytest.approx((15.5439, 73.7553))
    assert r["camera_make"] == "Apple" and r["source_kind"] == "phone"
    assert conn.execute("SELECT COUNT(*) FROM photo_embeddings WHERE photo_id=?", (r["id"],)).fetchone()[0] == 1

    c = client_for()
    idx = c.get("/api/photos/index").json()
    i = idx["ids"].index(r["id"])
    assert idx["flags"][i] & 4 and idx["dur"][i] == pytest.approx(3.0, abs=0.2)
    assert c.get(f"/api/thumb/{r['id']}?s=m").status_code == 200
    part = c.get(f"/api/photos/{r['id']}/video", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100     # seekable in the browser
    assert c.get("/api/photos/index", params={"media": "video"}).json()["ids"] == [r["id"]]
    conn.close()


def test_mp4_creation_time_is_utc_and_converted_to_local_time(ctx, tmp_path):
    root = tmp_path / "lib"
    make_video(root / "holiday.mp4", metadata={"creation_time": "2022-06-01T10:00:00.000000Z"})
    index(ctx, root)
    conn = ctx.connect()
    r = conn.execute("SELECT taken_local, date_source, date_confidence FROM photos").fetchone()
    assert r["taken_local"] == local_from_utc(2022, 6, 1, 10, 0).strftime("%Y-%m-%d %H:%M:%S")
    assert (r["date_source"], r["date_confidence"]) == ("video_meta_utc", "medium")
    conn.close()


def test_phone_filename_beats_utc_container_date(ctx, tmp_path):
    """Android: VID_20230105_101500.mp4 is local time; creation_time is UTC and zone-less."""
    root = tmp_path / "lib"
    make_video(root / "VID_20230105_101500.mp4", metadata={"creation_time": "2023-01-05T04:45:00.000000Z"})
    index(ctx, root)
    conn = ctx.connect()
    r = conn.execute("SELECT taken_local, date_source FROM photos").fetchone()
    assert (r["taken_local"], r["date_source"]) == ("2023-01-05 10:15:00", "filename")
    conn.close()


def test_rotated_video_reports_upright_dimensions(ctx, tmp_path):
    root = tmp_path / "lib"
    make_video(root / "portrait.mp4", size=(320, 240), rotation=90)
    index(ctx, root)
    conn = ctx.connect()
    r = conn.execute("SELECT width, height FROM photos").fetchone()
    assert (r["width"], r["height"]) == (240, 320)
    conn.close()


def test_unplayable_codec_is_transcoded_into_the_cache(ctx, tmp_path, client_for):
    root = tmp_path / "lib"
    src = make_video(root / "old_camcorder.avi", codec="mpeg4")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    index(ctx, root)
    conn = ctx.connect()
    pid, codec = conn.execute("SELECT id, video_codec FROM photos").fetchone()
    assert codec == "mpeg4"
    r = client_for().get(f"/api/photos/{pid}/video")
    assert r.status_code == 200 and r.content[4:8] == b"ftyp"
    with av.open(io.BytesIO(r.content)) as c:
        assert c.streams.video[0].codec_context.name == "h264"
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before       # original untouched
    assert list((ctx.paths.data / "cache" / "videos").rglob("*.mp4"))
    conn.close()


def test_video_is_never_a_near_duplicate_of_a_photo(ctx, tmp_path):
    """A video's hashes describe one frame; alone they must not pair it with a still."""
    from photointel.engine.duplicates import find_duplicates

    root = tmp_path / "lib"
    colour = (100, 150, 200)
    make_image(root / "still.jpg", size=(320, 240), colour=colour, taken=datetime(2024, 1, 1, 9, 0))
    make_video(root / "moving.mp4", colour=colour, seconds=2)
    index(ctx, root)
    conn = ctx.connect()
    ph = [r[0] for r in conn.execute("SELECT phash FROM photos ORDER BY id")]
    from photointel.hashing import hamming

    # The frame really does look like the photo: close enough for the semantic "near" rule
    # (embedding >= 0.985 with pHash within 12), which is what would pair them.
    assert hamming(ph[0], ph[1]) <= 12
    find_duplicates(ctx, conn)
    assert conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 0
    conn.close()


# ----------------------------------------------------------------- live & motion photos

def test_live_photo_pair_shows_as_one_item(ctx, tmp_path, client_for):
    root = tmp_path / "iphone"
    for i in range(4):   # enough sightings of this person for a cluster to form at all
        make_image(root / "other" / f"IMG_01{i}.JPG", colour=(100, 90, 60), taken=datetime(2024, 2, 1 + i, 9, 0),
                   noise=3)
    make_image(root / "IMG_0042.JPG", colour=(100, 90, 60), taken=datetime(2024, 2, 3, 12, 0, 0))
    make_video(root / "IMG_0042.MOV", seconds=2.5, colour=(100, 90, 60),
               metadata={"com.apple.quicktime.creationdate": "2024-02-03T12:00:01+0530"})
    index(ctx, root)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["live-photos", "people"])
    still = conn.execute("SELECT id, live_video_id FROM photos WHERE filename='IMG_0042.JPG'").fetchone()
    vid = conn.execute("SELECT id, live_component FROM photos WHERE filename='IMG_0042.MOV'").fetchone()
    assert still["live_video_id"] == vid["id"] and vid["live_component"] == 1

    c = client_for()
    idx = c.get("/api/photos/index").json()
    assert vid["id"] not in idx["ids"] and idx["flags"][idx["ids"].index(still["id"])] & 8
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE photo_id=? AND person_id IS NOT NULL",
                        (still["id"],)).fetchone()[0] == 1          # a person did form
    assert c.get(f"/api/photos/{still['id']}/motion").content[4:8] == b"ftyp"
    assert [p["id"] for p in c.get("/api/search", params={"q": "live photos"}).json()["photos"]] == [still["id"]]
    # the motion half's face is not a second sighting of the same person
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE photo_id=? AND person_id IS NOT NULL",
                        (vid["id"],)).fetchone()[0] == 0
    conn.close()


def test_same_name_video_from_another_moment_is_not_paired(ctx, tmp_path):
    root = tmp_path / "lib"
    make_image(root / "IMG_0001.JPG", taken=datetime(2021, 1, 1, 9, 0))
    make_video(root / "IMG_0001.MOV", metadata={"com.apple.quicktime.creationdate": "2023-07-07T09:00:00+0000"})
    index(ctx, root)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["live-photos"])
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE live_component=1").fetchone()[0] == 0
    conn.close()


def test_motion_photo_is_detected_and_its_video_served(ctx, tmp_path, client_for):
    root = tmp_path / "pixel"
    path, offset = make_motion_photo(root / "MVIMG_20240501_100000.jpg")
    index(ctx, root)
    conn = ctx.connect()
    r = conn.execute("SELECT id, motion_offset, media_type FROM photos").fetchone()
    assert r["media_type"] == "image" and r["motion_offset"] == offset
    body = client_for().get(f"/api/photos/{r['id']}/motion").content
    assert body == path.read_bytes()[offset:]
    with av.open(io.BytesIO(body)) as c:
        assert c.streams.video
    conn.close()


def test_motion_backfill_finds_photos_indexed_before_detection(ctx, tmp_path):
    root = tmp_path / "pixel"
    _, offset = make_motion_photo(root / "PXL_old.jpg")
    make_image(root / "plain.jpg")
    index(ctx, root)
    conn = ctx.connect()
    conn.execute("UPDATE photos SET motion_offset = NULL")        # as if indexed by an older version
    conn.commit()
    run_post_stages(ctx, conn, stages=["live-photos"])
    got = dict(conn.execute("SELECT filename, motion_offset FROM photos").fetchall())
    assert got == {"PXL_old.jpg": offset, "plain.jpg": 0}
    conn.close()


def test_ordinary_jpeg_containing_ftyp_bytes_is_not_a_motion_photo():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, "JPEG")
    assert video.find_motion_offset(buf.getvalue() + b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64) is None


# ----------------------------------------------------------------- albums

def test_album_lifecycle_through_the_api(ctx, library, client_for):
    index(ctx, library)
    c = client_for()
    ids = c.get("/api/photos/index").json()["ids"][:3]
    aid = c.post("/api/albums", json={"name": "Best of 2024", "photo_ids": ids[:2]}).json()["id"]
    c.post(f"/api/albums/{aid}/photos", json={"photo_ids": [ids[2]]})
    detail = c.get(f"/api/albums/{aid}").json()
    assert sorted(detail["photos"]["ids"]) == sorted(ids) and detail["photo_count"] == 3
    c.post(f"/api/albums/{aid}", json={"name": "Best of the year"})
    assert c.get("/api/albums").json()["albums"][0]["name"] == "Best of the year"
    c.post(f"/api/albums/{aid}/photos/remove", json={"photo_ids": [ids[0]]})
    assert ids[0] not in c.get(f"/api/albums/{aid}").json()["photos"]["ids"]
    assert c.delete(f"/api/albums/{aid}").status_code == 200
    assert c.get(f"/api/albums/{aid}").status_code == 404
    conn = ctx.connect()
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE id IN (?,?,?)", ids).fetchone()[0] == 3   # photos stay
    conn.close()


def test_album_shows_the_visible_copy_when_its_own_copy_is_hidden(ctx, library):
    """Hiding a duplicate (the Duplicates screen's only action) must not empty an album."""
    index(ctx, library)
    conn = ctx.connect()
    backup = conn.execute("SELECT id, sha256 FROM photos WHERE rel_path LIKE 'Backup/%'").fetchone()
    original = conn.execute("SELECT id FROM photos WHERE sha256=? AND id != ?",
                            (backup["sha256"], backup["id"])).fetchone()[0]
    aid = albums_mod.create_album(conn, "Kept", [backup["id"]])
    conn.execute("UPDATE photos SET hidden=1 WHERE id=?", (backup["id"],))
    conn.commit()
    assert albums_mod.album_photo_ids(conn, aid) == [original]
    conn.close()


def test_search_by_album_name(ctx, library, client_for):
    index(ctx, library)
    conn = ctx.connect()
    goa = [r[0] for r in conn.execute("SELECT id FROM photos WHERE rel_path LIKE 'Trips/Goa/%'")]
    albums_mod.create_album(conn, "Goa with friends", goa)
    conn.close()
    r = client_for().get("/api/search", params={"q": "photos in goa with friends"}).json()
    assert {c["kind"] for c in r["interpretation"]} >= {"album"}
    assert sorted(p["id"] for p in r["photos"]) == sorted(goa)


# ----------------------------------------------------------------- Google Takeout

def _sidecar(path: Path, taken: datetime | None = None, **extra) -> None:
    data = {"title": path.name, **extra}
    if taken:
        data["photoTakenTime"] = {"timestamp": str(int(taken.replace(tzinfo=timezone.utc).timestamp()))}
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def takeout(tmp_path):
    gp = tmp_path / "Takeout" / "Google Photos"
    year = gp / "Photos from 2019"
    # EXIF-dated photo whose sidecar disagrees: EXIF must win (measured, HANDOFF §4)
    make_image(year / "IMG_20190301_101010.jpg", taken=datetime(2019, 3, 1, 10, 10, 10), colour=(40, 90, 160))
    _sidecar(year / "IMG_20190301_101010.jpg.supplemental-metadata.json", taken=datetime(2020, 1, 8, 15, 46),
             favorited=True, description="Holi at the lake")
    # no date of its own: the sidecar is better than the extraction-day mtime
    make_image(year / "scan_of_grandma.jpg", colour=(40, 90, 160), camera=None)
    _sidecar(year / "scan_of_grandma.jpg.supplemental-metadata.json", taken=datetime(2019, 12, 25, 18, 0),
             geoData={"latitude": 17.385, "longitude": 78.4867, "altitude": 0})
    # long name: the sidecar name is truncated to fit 51 characters
    long = "PXL_20191231_235959123.NIGHT.long-description-name.jpg"
    make_image(year / long, colour=(40, 90, 160), taken=datetime(2019, 12, 31, 23, 59))
    # Takeout keeps whole sidecar names within 51 characters, ".json" included
    _sidecar(year / ((long + ".supplemental-metadata")[:46] + ".json"), taken=datetime(2019, 12, 31, 23, 59),
             trashed=True)
    # bracketed duplicate name: image(1).jpg <-> image.jpg.supplemental-metadata(1).json
    make_image(year / "image(1).jpg", colour=(40, 90, 160), taken=datetime(2019, 6, 1, 9, 0))
    _sidecar(year / "image.jpg.supplemental-metadata(1).json", taken=datetime(2019, 6, 1, 9, 0),
             description="The one with the cake")
    # an album folder: byte-identical copies plus metadata.json
    album = gp / "Goa Trip"
    album.mkdir(parents=True)
    (album / "IMG_20190301_101010.jpg").write_bytes((year / "IMG_20190301_101010.jpg").read_bytes())
    (album / "metadata.json").write_text(json.dumps({"title": "Goa Trip 2019", "description": "Beach week"}))
    return tmp_path / "Takeout"


@pytest.mark.parametrize("media,jsons,expected", [
    ("IMG_1.jpg", ["IMG_1.jpg.json"], "IMG_1.jpg.json"),
    ("IMG_1.jpg", ["IMG_1.jpg.supplemental-metadata.json"], "IMG_1.jpg.supplemental-metadata.json"),
    ("IMG_1.jpg", ["IMG_1.jpg.supplemental-me.json"], "IMG_1.jpg.supplemental-me.json"),
    ("IMG_1(2).jpg", ["IMG_1.jpg(2).json", "IMG_1.jpg.json"], "IMG_1.jpg(2).json"),
    ("IMG_1(2).jpg", ["IMG_1.jpg.supplemental-metadata(2).json"], "IMG_1.jpg.supplemental-metadata(2).json"),
    ("IMG_1-edited.jpg", ["IMG_1.jpg.supplemental-metadata.json"], "IMG_1.jpg.supplemental-metadata.json"),
    ("20030616.jpg", ["20030616.json"], "20030616.json"),
    ("IMG_1.jpg", ["IMG_10.jpg.json", "metadata.json"], None),
    ("IMG_1(2).jpg", ["IMG_1.jpg.json"], None),        # the (2) copy must not borrow the original's sidecar
])
def test_sidecar_name_matching(media, jsons, expected):
    assert takeout_mod._FolderSidecars(jsons).match(media) == expected


def test_takeout_import(ctx, takeout, client_for):
    index(ctx, takeout)
    conn = ctx.connect()
    out = run_post_stages(ctx, conn, stages=["takeout", "search-index"])
    assert "error" not in out["takeout"], out
    rows = {r["filename"] + "|" + r["folder"].split("/")[-1]: r for r in conn.execute("SELECT * FROM photos")}
    exif = rows["IMG_20190301_101010.jpg|Photos from 2019"]
    assert exif["taken_local"] == "2019-03-01 10:10:10" and exif["date_source"] == "exif"   # EXIF wins
    assert exif["favorite"] == 1 and exif["description"] == "Holi at the lake"
    scan = rows["scan_of_grandma.jpg|Photos from 2019"]
    assert scan["date_source"] == "takeout" and scan["date_confidence"] == "low"
    assert scan["taken_local"] == datetime.fromtimestamp(
        datetime(2019, 12, 25, 18, 0, tzinfo=timezone.utc).timestamp()).strftime("%Y-%m-%d %H:%M:%S")
    assert (scan["gps_lat"], scan["location_source"], scan["location_confidence"]) == (17.385, "takeout", "medium")
    long = [r for k, r in rows.items() if k.startswith("PXL_20191231")][0]
    assert long["hidden"] == 1                                  # trashed in Google Photos -> hidden here
    assert rows["image(1).jpg|Photos from 2019"]["description"] == "The one with the cake"

    album = conn.execute("SELECT * FROM albums").fetchone()
    assert (album["name"], album["description"], album["source"]) == ("Goa Trip 2019", "Beach week", "takeout")
    assert len(albums_mod.album_photo_ids(conn, album["id"])) == 1

    c = client_for()
    hit = c.get("/api/search", params={"q": '"cake"'}).json()
    assert [p["id"] for p in hit["photos"]] == [rows["image(1).jpg|Photos from 2019"]["id"]]
    conn.close()


def test_takeout_location_keeps_its_provenance_after_geocoding(ctx, takeout):
    """Google's estimated location must not become 'from GPS, high confidence'."""
    geo = ctx.paths.geo
    row = ["1269843", "Hyderabad", "Hyderabad", "", "17.38405", "78.45636", "P", "PPLA", "IN", "", "40", "",
           "", "", "3597816", "", "536", "Asia/Kolkata", "2020-01-01"]
    (geo / "cities500.txt").write_text("\t".join(row) + "\n", encoding="utf-8")
    (geo / "admin1CodesASCII.txt").write_text("IN.40\tTelangana\tTelangana\t1254788\n", encoding="utf-8")
    (geo / "countryInfo.txt").write_text("IN\tIND\t356\tIN\tIndia\n", encoding="utf-8")
    from photointel.geo import ReverseGeocoder

    ReverseGeocoder._instance = None
    index(ctx, takeout)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["takeout", "geocode"])
    r = conn.execute("SELECT p.location_source, p.location_confidence, pl.city FROM photos p "
                     "JOIN places pl ON pl.id = p.place_id WHERE p.filename='scan_of_grandma.jpg'").fetchone()
    assert tuple(r) == ("takeout", "medium", "Hyderabad")
    ReverseGeocoder._instance = None
    conn.close()


def test_takeout_flags_apply_once_and_never_override_the_user(ctx, takeout):
    index(ctx, takeout)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["takeout"])
    pid = conn.execute("SELECT id FROM photos WHERE favorite=1").fetchone()[0]
    conn.execute("UPDATE photos SET favorite=0 WHERE id=?", (pid,))      # the user un-favourites it here
    conn.commit()
    run_post_stages(ctx, conn, stages=["takeout"])
    assert conn.execute("SELECT favorite FROM photos WHERE id=?", (pid,)).fetchone()[0] == 0
    conn.close()


def test_deleted_takeout_album_is_not_reimported(ctx, takeout):
    index(ctx, takeout)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["takeout"])
    aid = conn.execute("SELECT id FROM albums").fetchone()[0]
    albums_mod.delete_album(conn, aid)
    run_post_stages(ctx, conn, stages=["takeout"])
    assert albums_mod.list_albums(conn) == []
    conn.close()


def test_takeout_people_names_are_suggested_not_applied(ctx, tmp_path):
    """Names are suggested per person only when the evidence is specific to that person."""
    year = tmp_path / "Takeout" / "Google Photos" / "Photos from 2020"

    def shoot(name, colour, people, n, start):
        for i in range(n):
            f = year / f"{name}_{i}.jpg"
            make_image(f, colour=colour, taken=datetime(2020, 1, 1 + start + i, 12, 0), noise=4)
            _sidecar(f.with_name(f.name + ".supplemental-metadata.json"), taken=datetime(2020, 1, 1),
                     people=[{"name": p} for p in people])

    shoot("a", (100, 90, 60), ["Priya"], 5, 0)        # identity 2: always labelled Priya
    shoot("b", (180, 90, 60), ["Ravi"], 4, 6)         # identity 4: always labelled Ravi
    shoot("c", (20, 90, 60), ["Ravi", "Meena"], 5, 12)  # identity 0: Ravi's name here is someone else
    index(ctx, tmp_path / "Takeout")
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["takeout", "people"])
    person_of = {}
    for letter in "abc":
        person_of[letter] = conn.execute(
            "SELECT f.person_id FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.filename=?",
            (f"{letter}_0.jpg",)).fetchone()[0]
    got = {s["person_id"]: s["name"] for s in takeout_mod.name_suggestions(conn)}
    assert got[person_of["a"]] == "Priya"
    assert got.get(person_of["b"]) != "Ravi" or got.get(person_of["c"]) != "Ravi"   # one name, one person
    assert got.get(person_of["c"]) == "Meena"
    assert conn.execute("SELECT COUNT(*) FROM persons WHERE name IS NOT NULL").fetchone()[0] == 0  # nothing applied

    takeout_mod.dismiss_name_suggestion(conn, person_of["a"], "Priya")
    assert "Priya" not in {s["name"] for s in takeout_mod.name_suggestions(conn)}
    conn.close()


# ----------------------------------------------------------------- OCR

class FakeOcr:
    def __init__(self, text_for: dict[str, str]):
        self.text_for = text_for

    def read(self, rgb):
        mean = int(rgb[..., 0].mean())
        return next((t for k, t in self.text_for.items() if abs(int(k) - mean) < 6), "")


def test_ocr_reads_likely_text_photos_and_quoted_search_finds_them(ctx, library, client_for):
    from photointel.engine import ocr as ocr_mod

    index(ctx, library)
    conn = ctx.connect()
    out = ocr_mod.ocr_photos(ctx, conn, engine=FakeOcr({"240": "Reliance Fresh INVOICE total 1,240"}))
    assert out["read"] == 1                     # only the screenshot is a likely-text photo by default
    run_post_stages(ctx, conn, stages=["search-index"])
    c = client_for()
    shot = conn.execute("SELECT id FROM photos WHERE source_kind='screenshot'").fetchone()[0]
    for q in ('"reliance fresh"', "screenshot that says invoice"):
        r = c.get("/api/search", params={"q": q}).json()
        assert [p["id"] for p in r["photos"]] == [shot], q
        assert any(ch["kind"] == "text" for ch in r["interpretation"])
    assert c.get("/api/search", params={"q": '"bigbasket"'}).json()["photos"] == []
    # An automatic tag next to the text only ranks: this screenshot was never tagged "receipt".
    assert not conn.execute("SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
                            "WHERE pt.photo_id = ? AND t.name = 'receipt' AND pt.score >= 2", (shot,)).fetchone()
    r = c.get("/api/search", params={"q": "receipt that says invoice"}).json()
    assert [p["id"] for p in r["photos"]] == [shot]
    # read once: a second run has nothing left to do
    assert ocr_mod.ocr_photos(ctx, conn, engine=FakeOcr({}))["read"] == 0
    conn.close()


def test_real_ocr_reads_rendered_text(tmp_path):
    """The real RapidOCR models on a rendered sign, CPU only."""
    from PIL import ImageDraw, ImageFont

    from photointel.engine import ocr as ocr_mod

    if not ocr_mod.available():
        pytest.skip("rapidocr_onnxruntime not installed")
    img = Image.new("RGB", (900, 300), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=72)
    except TypeError:
        pytest.skip("Pillow without scalable default font")
    draw.text((40, 100), "MEMORIA 2024", fill="black", font=font)
    text = ocr_mod.OcrEngine().read(np.asarray(img))
    assert "MEMORIA" in text.upper() and "2024" in text


# ----------------------------------------------------------------- user tags

def test_user_tags_filter_search_and_survive_retagging(ctx, library, client_for):
    from photointel.engine.tags import tag_photos

    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["tags"])
    c = client_for()
    goa = [r[0] for r in conn.execute("SELECT id FROM photos WHERE rel_path LIKE 'Trips/Goa/%' ORDER BY id")]
    c.post("/api/photos/tags", json={"photo_ids": goa, "name": "Anniversary"})
    r = c.get("/api/search", params={"q": "anniversary photos"}).json()
    assert sorted(p["id"] for p in r["photos"]) == goa
    assert any(t["by_user"] for t in c.get(f"/api/photos/{goa[0]}").json()["tags"])

    c.post("/api/photos/tags/remove", json={"photo_ids": [goa[0]], "name": "anniversary"})
    r = c.get("/api/search", params={"q": "anniversary"}).json()
    assert sorted(p["id"] for p in r["photos"]) == goa[1:]

    # Taking off a tag the *tagger* added must survive a full automatic re-tag.
    photo, auto_tag = conn.execute(
        "SELECT pt.photo_id, t.name FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
        "WHERE pt.source = 'semantic' ORDER BY pt.score DESC LIMIT 1").fetchone()
    c.post("/api/photos/tags/remove", json={"photo_ids": [photo], "name": auto_tag})
    tag_photos(ctx, conn, force=True)              # e.g. after a vocabulary change
    assert auto_tag not in {t["name"] for t in c.get(f"/api/photos/{photo}").json()["tags"]}
    # ...and a tag the user added to a photo the tagger also scores keeps the user's score.
    c.post("/api/photos/tags", json={"photo_ids": [photo], "name": auto_tag})
    tag_photos(ctx, conn, force=True)
    assert conn.execute("SELECT pt.source FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
                        "WHERE pt.photo_id = ? AND t.name = ?", (photo, auto_tag)).fetchone()[0] == "user"
    conn.close()


def test_videos_query_returns_only_videos(ctx, library, client_for):
    make_video(library / "Clips" / "VID_20240309_120000.mp4")
    index(ctx, library)
    conn = ctx.connect()
    vid = conn.execute("SELECT id FROM photos WHERE media_type='video'").fetchone()[0]
    r = client_for().get("/api/search", params={"q": "videos"}).json()
    assert [p["id"] for p in r["photos"]] == [vid]
    conn.close()
