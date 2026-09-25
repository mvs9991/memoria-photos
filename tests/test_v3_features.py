"""Ratings, stacks, birthdays and ages, date/place corrections, smart albums, XMP export."""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from photointel import db
from photointel.engine import corrections, stacks
from photointel.engine import people as people_mod
from photointel.metadata import ts_to_naive
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from tests.conftest import make_image


def index(ctx, root) -> None:
    Indexer(ctx, workers=2).run(roots=[str(root)])


@pytest.fixture
def client(ctx):
    from photointel.api.app import create_app

    with TestClient(create_app(ctx)) as c:
        yield c


def _ids(c, **params):
    return c.get("/api/photos/index", params=params).json()


# ----------------------------------------------------------------- schema

def test_v2_database_gains_v3_columns(tmp_path):
    path = tmp_path / "library.db"
    conn = db.init_db(path)
    conn.execute("DROP INDEX IF EXISTS ix_photos_stack")
    for table, cols in db.V3_COLUMNS.items():
        for name, _ in cols:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    db.set_meta(conn, "schema_version", 2)
    conn.commit()
    conn.close()
    conn = db.init_db(path)
    for table, cols in db.V3_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert {n for n, _ in cols} <= have
    assert db.get_meta(conn, "schema_version") == "3"
    conn.close()


# ----------------------------------------------------------------- ratings

def test_ratings_filter_search_and_lead_best_first(ctx, library, client):
    index(ctx, library)
    ids = _ids(client)["ids"]
    low_quality = _ids(client, order="quality")["ids"][-1]
    other = next(i for i in ids if i != low_quality)
    client.post("/api/photos/rate", json={"photo_ids": [low_quality], "rating": 5})
    client.post("/api/photos/rate", json={"photo_ids": [other], "rating": 3})
    assert _ids(client, order="quality")["ids"][0] == low_quality        # your stars outrank the score
    assert _ids(client, min_rating=4)["ids"] == [low_quality]
    assert set(_ids(client, min_rating=1)["ids"]) == {low_quality, other}
    r = client.get("/api/search", params={"q": "5 star photos"}).json()
    assert [p["id"] for p in r["photos"]] == [low_quality]
    assert {p["id"] for p in client.get("/api/search", params={"q": "rated"}).json()["photos"]} == {low_quality, other}
    assert client.get(f"/api/photos/{low_quality}").json()["rating"] == 5
    assert client.post("/api/photos/rate", json={"photo_ids": [other], "rating": 6}).status_code == 400


# ----------------------------------------------------------------- stacks

def _burst_library(root):
    t = datetime(2024, 5, 5, 10, 0, 0)
    for i in range(3):        # one scene, three frames a second apart
        make_image(root / f"IMG_{i}.jpg", colour=(90, 140, 190), taken=t + timedelta(seconds=i), noise=6)
    make_image(root / "IMG_later.jpg", colour=(90, 140, 190), taken=t + timedelta(seconds=40), noise=6)
    # a second later but a different scene: two snaps, not a burst
    make_image(root / "IMG_other_a.jpg", colour=(200, 60, 40), taken=t + timedelta(minutes=5), noise=6)
    other = make_image(root / "IMG_other_b.jpg", size=(800, 600), colour=(30, 30, 30),
                       taken=t + timedelta(minutes=5, seconds=1), noise=6)
    from PIL import Image, ImageDraw

    img = Image.open(other)
    d = ImageDraw.Draw(img)
    for x in range(0, 800, 40):
        d.rectangle([x, 0, x + 18, 600], fill=(240, 240, 240))
    img.save(other, "JPEG", exif=img.info.get("exif", b""))
    os.utime(other, (t.timestamp(), t.timestamp()))


def test_burst_frames_fold_into_one_stack(ctx, tmp_path, client):
    root = tmp_path / "burst"
    _burst_library(root)
    index(ctx, root)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["quality", "stacks"])
    burst = [r[0] for r in conn.execute("SELECT id FROM photos WHERE filename LIKE 'IMG__.jpg' ORDER BY id")]
    cover = conn.execute("SELECT stack_id FROM photos WHERE id = ?", (burst[0],)).fetchone()[0]
    assert cover in burst
    assert {r[0] for r in conn.execute("SELECT id FROM photos WHERE stack_id = ?", (cover,))} == set(burst)
    others = conn.execute("SELECT COUNT(*) FROM photos WHERE filename LIKE 'IMG_other%' AND stack_id IS NOT NULL")
    assert others.fetchone()[0] == 0          # a second apart but different scenes
    assert conn.execute("SELECT stack_id FROM photos WHERE filename='IMG_later.jpg'").fetchone()[0] is None

    folded = _ids(client, collapse_stacks=True)
    assert len(folded["ids"]) == 6 - 2 and cover in folded["ids"]
    i = folded["ids"].index(cover)
    assert folded["flags"][i] & 16 and folded["stack"][i] == 3
    assert len(_ids(client)["ids"]) == 6          # search and filtered views still see every frame
    assert client.get(f"/api/stacks/{cover}").json()["members"][0] == cover

    other = next(b for b in burst if b != cover)
    client.post(f"/api/stacks/{cover}/cover", json={"photo_id": other})
    assert other in _ids(client, collapse_stacks=True)["ids"]
    client.post(f"/api/stacks/{other}/unstack")
    run_post_stages(ctx, conn, stages=["stacks"])                  # a split stack stays split
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE stack_id IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_raw_and_jpeg_of_one_shot_stack_under_the_jpeg(ctx, tmp_path):
    root = tmp_path / "cam"
    make_image(root / "DSC_0001.JPG", taken=datetime(2024, 6, 1, 8, 0))
    make_image(root / "DSC_0002.JPG", colour=(10, 200, 40), taken=datetime(2024, 6, 1, 9, 0))
    make_image(root / "DSC_0001.tmp.jpg", colour=(10, 200, 40), taken=datetime(2024, 6, 1, 8, 0, 30))
    index(ctx, root)
    conn = ctx.connect()
    # No real RAW file exists here (see HANDOFF §9); stand one in by name, which is all pairing reads.
    conn.execute("UPDATE photos SET filename='DSC_0001.NEF', ext='.nef' WHERE filename='DSC_0001.tmp.jpg'")
    conn.commit()
    stacks.build_stacks(conn)
    jpg, raw = (conn.execute("SELECT id, stack_id, stack_hidden FROM photos WHERE filename=?", (n,)).fetchone()
                for n in ("DSC_0001.JPG", "DSC_0001.NEF"))
    assert jpg["stack_id"] == jpg["id"] == raw["stack_id"] and (jpg["stack_hidden"], raw["stack_hidden"]) == (0, 1)
    assert conn.execute("SELECT stack_id FROM photos WHERE filename='DSC_0002.JPG'").fetchone()[0] is None
    conn.close()


# ----------------------------------------------------------------- birthdays & ages

def test_age_search_and_face_ages(ctx, library, client):
    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["people"])
    goa = conn.execute("SELECT f.person_id FROM faces f JOIN photos p ON p.id = f.photo_id "
                       "WHERE p.rel_path LIKE 'Trips/Goa/%' AND f.person_id IS NOT NULL").fetchone()[0]
    people_mod.rename_person(conn, goa, "Anu")
    assert client.post(f"/api/people/{goa}/flags", json={"birth_date": "not a date"}).status_code == 400
    client.post(f"/api/people/{goa}/flags", json={"birth_date": "2019-07-01"})
    r = client.get("/api/search", params={"q": "Anu at age 5"}).json()          # Goa photos: 20 July 2024
    assert r["total"] > 0 and any(c["label"] == "age 5" for c in r["interpretation"])
    assert client.get("/api/search", params={"q": "Anu at age 3"}).json()["total"] == 0
    pid = r["photos"][0]["id"]
    face = next(f for f in client.get(f"/api/photos/{pid}").json()["faces"] if f["person_id"] == goa)
    assert face["age"] == 5
    assert client.get(f"/api/people/{goa}").json()["birth_date"] == "2019-07-01"
    conn.close()


def test_birthday_memories_surface_photos_from_past_birthdays(ctx, library, client, monkeypatch):
    from photointel.api import routes_library

    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["people"])
    goa = conn.execute("SELECT f.person_id FROM faces f JOIN photos p ON p.id = f.photo_id "
                       "WHERE p.rel_path LIKE 'Trips/Goa/%' AND f.person_id IS NOT NULL").fetchone()[0]
    people_mod.rename_person(conn, goa, "Anu")
    people_mod.set_birth_date(conn, goa, "2019-07-21")       # Goa photos were taken on 20 July

    class Today(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 18, 9, 0)

    monkeypatch.setattr(routes_library, "datetime", Today)
    sections = client.get("/api/memories").json()["sections"]
    b = sections[0]
    assert b["kind"] == "birthday" and b["title"] == "Anu's birthday" and "turns 7" in b["subtitle"]
    assert b["groups"][0]["title"] == "2024" and b["groups"][0]["photo_ids"]
    conn.close()


# ----------------------------------------------------------------- date & place corrections

def _geo(ctx):
    geo = ctx.paths.geo
    row = ["1269843", "Hyderabad", "Hyderabad", "", "17.38405", "78.45636", "P", "PPLA", "IN", "", "40", "",
           "", "", "3597816", "", "536", "Asia/Kolkata", "2020-01-01"]
    (geo / "cities500.txt").write_text("\t".join(row) + "\n", encoding="utf-8")
    (geo / "admin1CodesASCII.txt").write_text("IN.40\tTelangana\tTelangana\t1254788\n", encoding="utf-8")
    (geo / "countryInfo.txt").write_text("IN\tIND\t356\tIN\tIndia\n", encoding="utf-8")
    from photointel.geo import ReverseGeocoder

    ReverseGeocoder._instance = None


def test_date_correction_survives_reindexing_and_can_be_cleared(ctx, library, client):
    index(ctx, library)
    conn = ctx.connect()
    goa = [r[0] for r in conn.execute("SELECT id FROM photos WHERE rel_path LIKE 'Trips/Goa/%' ORDER BY taken_ts")]
    before = [conn.execute("SELECT taken_ts FROM photos WHERE id=?", (p,)).fetchone()[0] for p in goa]
    out = client.post("/api/photos/correct-date",
                      json={"photo_ids": goa, "shift_seconds": -3600, "rebuild_events": False}).json()
    assert out["corrected"] == len(goa)
    after = [conn.execute("SELECT taken_ts, date_source FROM photos WHERE id=?", (p,)).fetchone() for p in goa]
    assert [a[0] for a in after] == [b - 3600 for b in before] and {a[1] for a in after} == {"user"}

    target = library / conn.execute("SELECT rel_path FROM photos WHERE id=?", (goa[0],)).fetchone()[0]
    make_image(target, colour=(200, 120, 61), taken=datetime(2024, 7, 20, 9, 0))   # file changes on disk
    index(ctx, library)
    assert conn.execute("SELECT taken_ts, date_source FROM photos WHERE id=?", (goa[0],)).fetchone()[1] == "user"

    client.post("/api/photos/correct-date", json={"photo_ids": [goa[1]], "taken_local": "2020-02-29 18:30",
                                                  "rebuild_events": False})
    assert conn.execute("SELECT taken_local FROM photos WHERE id=?", (goa[1],)).fetchone()[0] == "2020-02-29 18:30:00"
    client.post("/api/photos/corrections/clear", json={"photo_ids": [goa[1]]})
    index(ctx, library)
    assert conn.execute("SELECT date_source FROM photos WHERE id=?", (goa[1],)).fetchone()[0] == "exif"
    conn.close()


def test_location_correction_is_geocoded_and_labelled_as_yours(ctx, library, client):
    _geo(ctx)
    index(ctx, library)
    conn = ctx.connect()
    shot = conn.execute("SELECT id FROM photos WHERE source_kind='screenshot'").fetchone()[0]
    out = client.post("/api/photos/correct-location",
                      json={"photo_ids": [shot], "lat": 17.39, "lon": 78.47, "rebuild_events": False}).json()
    assert out["corrected"] == 1
    run_post_stages(ctx, conn, stages=["geocode"])
    r = conn.execute("SELECT p.location_source, p.location_confidence, pl.city FROM photos p "
                     "JOIN places pl ON pl.id = p.place_id WHERE p.id=?", (shot,)).fetchone()
    assert tuple(r) == ("user", "high", "Hyderabad")
    from photointel.geo import ReverseGeocoder

    ReverseGeocoder._instance = None
    conn.close()


# ----------------------------------------------------------------- smart albums

def test_smart_album_follows_its_search(ctx, library, client):
    index(ctx, library)
    aid = client.post("/api/albums", json={"name": "All my screenshots", "query": "screenshots"}).json()["id"]
    first = client.get(f"/api/albums/{aid}").json()
    assert first["kind"] == "smart" and first["photo_count"] == 1
    assert client.post(f"/api/albums/{aid}/photos", json={"photo_ids": [1]}).status_code == 400
    make_image(library / "Pictures" / "Screenshots" / "Screenshot_20240611-101010.png", size=(1080, 2340),
               colour=(230, 230, 250), fmt="PNG", camera=None)
    index(ctx, library)
    assert client.get(f"/api/albums/{aid}").json()["photo_count"] == 2              # it updates itself
    listed = next(a for a in client.get("/api/albums").json()["albums"] if a["id"] == aid)
    assert listed["photo_count"] == 2 and listed["query"] == "screenshots"


def test_smart_album_named_like_its_query_still_finds_it(ctx, library, client):
    """'Save as smart album' names the album after the search. The album's name must not then
    capture its own query as "photos in album Screenshots", which a smart album cannot answer."""
    index(ctx, library)
    aid = client.post("/api/albums", json={"name": "Screenshots", "query": "screenshots"}).json()["id"]
    assert client.get(f"/api/albums/{aid}").json()["photo_count"] == 1
    assert client.get("/api/search", params={"q": "screenshots"}).json()["total"] == 1


# ----------------------------------------------------------------- XMP export

X = {"rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "dc": "http://purl.org/dc/elements/1.1/",
     "xmp": "http://ns.adobe.com/xap/1.0/", "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
     "lr": "http://ns.adobe.com/lightroom/1.0/"}


def test_xmp_export_carries_people_tags_and_ratings(ctx, library, client, tmp_path):
    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["people", "tags"])
    goa_person = conn.execute("SELECT f.person_id FROM faces f JOIN photos p ON p.id = f.photo_id "
                              "WHERE p.rel_path LIKE 'Trips/Goa/%' AND f.person_id IS NOT NULL").fetchone()[0]
    people_mod.rename_person(conn, goa_person, "Anu & <Co>")
    photo = conn.execute("SELECT id, rel_path FROM photos WHERE rel_path LIKE 'Trips/Goa/%' ORDER BY id").fetchone()
    client.post("/api/photos/rate", json={"photo_ids": [photo["id"]], "rating": 4})
    client.post("/api/photos/tags", json={"photo_ids": [photo["id"]], "name": "Beach day"})

    assert client.post("/api/export/xmp", json={"folder": str(library / "exports")}).status_code == 400
    out = tmp_path / "xmp-out"
    res = client.post("/api/export/xmp", json={"folder": str(out)}).json()
    assert res["written"] >= 1
    sidecar = out / library.name / (photo["rel_path"] + ".xmp")
    tree = ET.parse(sidecar)                               # well-formed, names escaped
    desc = tree.find(".//rdf:Description", X)
    assert desc.get(f"{{{X['xmp']}}}Rating") == "4"
    subjects = {li.text for li in tree.findall(".//dc:subject/rdf:Bag/rdf:li", X)}
    assert {"beach day", "Anu & <Co>"} <= subjects
    assert "People|Anu & <Co>" in {li.text for li in tree.findall(".//lr:hierarchicalSubject/rdf:Bag/rdf:li", X)}
    assert tree.find(".//mwg-rs:RegionList/rdf:Bag/rdf:li/mwg-rs:Name", X).text == "Anu & <Co>"
    # automatic tags stay out unless asked for
    auto = conn.execute("SELECT t.name FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
                        "WHERE pt.photo_id = ? AND pt.source = 'semantic' AND pt.score >= 2.5", (photo["id"],)).fetchall()
    for (name,) in auto:
        assert name not in subjects
    assert not list(library.rglob("*.xmp"))               # nothing was written beside the originals
    conn.close()
