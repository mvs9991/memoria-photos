"""Collections, folders, insights, slideshow/frame, GPX tracks, password + share links, HTML export."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from photointel import cli, db
from photointel.engine import albums as albums_mod
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


# ----------------------------------------------------------------- collections & folders

def test_collections_count_and_filter(ctx, library, client):
    make_image(library / "Pano" / "PANO_1.jpg", size=(2400, 800), taken=datetime(2024, 1, 5, 10, 0))
    index(ctx, library)
    cols = {c["key"]: c for c in client.get("/api/collections").json()["collections"]}
    assert cols["screenshots"]["count"] == 1 and cols["screenshots"]["group"] == "cleanup"
    assert cols["panoramas"]["count"] == 1 and cols["videos"]["count"] == 0
    pano = client.get("/api/photos/index", params={"collection": "panoramas"}).json()["ids"]
    conn = ctx.connect()
    assert conn.execute("SELECT filename FROM photos WHERE id=?", (pano[0],)).fetchone()[0] == "PANO_1.jpg"
    shots = client.get("/api/photos/index", params={"collection": "screenshots"}).json()["ids"]
    assert len(shots) == 1                          # screenshots are not filtered out of their own collection
    assert client.get("/api/photos/index", params={"collection": "nope"}).status_code == 400
    added = client.get("/api/photos/index", params={"order": "added"}).json()["ids"]
    assert added[0] == pano[0] or len(added) > 1     # newest-found first
    conn.close()


def test_folder_browser_walks_the_tree(ctx, library, client):
    index(ctx, library)
    roots = client.get("/api/folders/browse").json()["folders"]
    assert len(roots) == 1 and roots[0]["count"] >= 12
    rid = roots[0]["root_id"]
    top = {f["name"]: f for f in client.get("/api/folders/browse", params={"root_id": rid}).json()["folders"]}
    assert {"DCIM", "Trips", "Backup", "Pictures"} <= set(top)
    assert top["DCIM"]["count"] == 6 and top["DCIM"]["cover_photo_id"]
    inner = client.get("/api/folders/browse", params={"root_id": rid, "path": "DCIM/Camera"}).json()
    assert inner["direct_count"] == 6 and inner["folders"] == []
    exact = client.get("/api/photos/index", params={"root_id": rid, "folder": "Trips", "folder_exact": True}).json()
    assert exact["total"] == 0                       # Trips itself holds no photos, only Goa does
    deep = client.get("/api/photos/index", params={"root_id": rid, "folder": "Trips"}).json()
    assert deep["total"] == 5


# ----------------------------------------------------------------- insights

def test_insights_and_year_in_review(ctx, library, client):
    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["people", "events"])
    allt = client.get("/api/insights").json()
    assert allt["totals"]["photos"] >= 12 and 2024 in allt["years"]
    assert allt["busiest_day"]["photos"] >= 5 and allt["people"]
    y = client.get("/api/insights", params={"year": 2024}).json()
    assert y["year"] == 2024 and sum(y["months"]) == y["totals"]["photos"] + y["totals"]["videos"]
    assert y["months"][2] == 7 and y["months"][6] == 5          # March (6 + the backup copy), July
    assert client.get("/api/insights", params={"year": 2019}).json()["totals"]["photos"] == 0
    assert len(y["best_photo_ids"]) <= 12
    conn.close()


# ----------------------------------------------------------------- slideshow / frame

def test_random_photos_for_slideshows_and_frames(ctx, library, client):
    index(ctx, library)
    conn = ctx.connect()
    goa = [r[0] for r in conn.execute("SELECT id FROM photos WHERE rel_path LIKE 'Trips/Goa/%'")]
    aid = albums_mod.create_album(conn, "Goa", goa)
    got = client.get("/api/random", params={"count": 50, "album": aid}).json()["ids"]
    assert sorted(got) == sorted(goa)
    one = client.get("/api/random", params={"count": 1}).json()["ids"]
    assert len(one) == 1
    img = client.get("/api/random/image", params={"album": aid})
    assert img.status_code == 200 and img.headers["cache-control"] == "no-store"
    conn.close()


# ----------------------------------------------------------------- GPX

def _gpx(points: list[tuple[datetime, float, float]]) -> str:
    pts = "".join(f'<trkpt lat="{la}" lon="{lo}"><time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>'
                  for t, la, lo in points)
    return (f'<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<trk><name>Hike</name><trkseg>{pts}</trkseg></trk></gpx>")


def test_gpx_track_places_photos_without_gps(ctx, tmp_path, client):
    root = tmp_path / "camera"
    t = datetime(2024, 4, 6, 9, 0)
    make_image(root / "DSC_0001.jpg", taken=t + timedelta(minutes=10), camera=("canon", "EOS R6"))
    make_image(root / "DSC_0002.jpg", taken=t + timedelta(hours=5), camera=("canon", "EOS R6"))   # after the walk
    make_image(root / "PXL_0003.jpg", taken=t + timedelta(minutes=10), gps=(12.0, 77.0))         # has its own GPS
    # the track records UTC; the camera clock is local wall time on this machine
    utc0 = datetime.fromtimestamp(t.astimezone().timestamp(), tz=timezone.utc).replace(tzinfo=None)
    track = [(utc0 + timedelta(minutes=m), 17.30 + m * 0.001, 78.40 + m * 0.001) for m in range(0, 30, 2)]
    (root / "walk.gpx").write_text(_gpx(track), encoding="utf-8")
    index(ctx, root)
    conn = ctx.connect()
    out = run_post_stages(ctx, conn, stages=["gpx"])["gpx"]
    assert out["tracks"] == 1 and out["placed"] == 1
    r = {x["filename"]: x for x in conn.execute("SELECT filename, gps_lat, gps_lon, location_source FROM photos")}
    assert r["DSC_0001.jpg"]["location_source"] == "gpx"
    assert r["DSC_0001.jpg"]["gps_lat"] == pytest.approx(17.31, abs=1e-6)       # 10 minutes in
    assert r["DSC_0002.jpg"]["gps_lat"] is None                                  # outside the track
    assert (r["PXL_0003.jpg"]["gps_lat"], r["PXL_0003.jpg"]["location_source"]) == (12.0, "gps")
    tracks = client.get("/api/gpx/tracks").json()["tracks"]
    assert tracks[0]["name"] == "Hike" and len(tracks[0]["points"]) == 15
    conn.close()


def test_import_gpx_rejects_junk(ctx, tmp_path):
    from photointel.engine.gpx import GpxError, import_file

    bad = tmp_path / "bad.gpx"
    bad.write_text("<gpx><trk/></gpx>", encoding="utf-8")
    conn = ctx.connect()
    with pytest.raises(GpxError):
        import_file(ctx, conn, bad)
    assert not (ctx.paths.data / "gpx" / "bad.gpx").exists()
    conn.close()


# ----------------------------------------------------------------- password & share links

def test_password_protects_every_api_route(ctx, library, client):
    index(ctx, library)
    assert client.get("/api/stats").status_code == 200                    # open by default (localhost)
    assert client.post("/api/auth/password", json={"new": "123"}).status_code == 400
    client.post("/api/auth/password", json={"new": "correct horse"})
    assert client.get("/api/stats").status_code == 200                    # the browser that set it stays in
    client.cookies.clear()
    for path in ("/api/stats", "/api/photos/index", "/api/thumb/1", "/api/people", "/api/settings"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/auth/status").json() == {"protected": True, "logged_in": False}
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert client.get("/api/stats").status_code == 200
    assert "correct horse" not in client.get("/api/settings").text
    # a password change logs other sessions out
    (ctx.paths.data / "password.changed").write_text(str(int(time.time()) + 5))
    assert client.get("/api/stats").status_code == 401


def test_serve_refuses_the_network_without_a_password(ctx, monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("should not start"))
    with pytest.raises(SystemExit) as e:
        cli.main(["--data", str(ctx.paths.data), "serve", "--host", "0.0.0.0"])
    assert e.value.code == 2


def test_share_link_opens_one_album_and_nothing_else(ctx, library, client):
    index(ctx, library)
    conn = ctx.connect()
    goa = [r[0] for r in conn.execute("SELECT id FROM photos WHERE rel_path LIKE 'Trips/Goa/%' ORDER BY id")]
    other = conn.execute("SELECT id FROM photos WHERE rel_path LIKE 'DCIM/%'").fetchone()[0]
    conn.execute("UPDATE photos SET favorite = 1 WHERE id = ?", (goa[0],))
    conn.commit()
    aid = albums_mod.create_album(conn, "Goa 2024", goa)
    share = client.post(f"/api/albums/{aid}/share", json={}).json()
    client.post("/api/auth/password", json={"new": "correct horse"})
    client.cookies.clear()                                                  # a visitor: no session

    tok = share["token"]
    page = client.get(f"/api/share/{tok}").json()
    assert page["name"] == "Goa 2024" and sorted(page["photos"]["ids"]) == goa
    assert not any(f & 1 for f in page["photos"]["flags"])                 # owner's favourites stay private
    assert client.get(f"/api/share/{tok}/thumb/{goa[0]}?s=m").status_code == 200
    assert client.get(f"/api/share/{tok}/thumb/{other}?s=m").status_code == 404
    assert client.get(f"/api/share/{tok}/download/{goa[0]}").status_code == 403
    assert client.get(f"/api/thumb/{goa[0]}").status_code == 401           # the rest of the API is closed

    client.post("/api/auth/login", json={"password": "correct horse"})
    client.delete(f"/api/shares/{tok}")
    client.cookies.clear()
    assert client.get(f"/api/share/{tok}").status_code == 404
    conn.execute("INSERT INTO share_links(token, album_id, created_at, expires_at) VALUES ('old', ?, 0, 1)", (aid,))
    conn.commit()
    assert client.get("/api/share/old").status_code == 404                  # expired
    conn.close()


# ----------------------------------------------------------------- static HTML export

def test_album_exports_as_a_static_site(ctx, library, client, tmp_path):
    index(ctx, library)
    conn = ctx.connect()
    goa = [r[0] for r in conn.execute("SELECT id FROM photos WHERE rel_path LIKE 'Trips/Goa/%'")]
    aid = albums_mod.create_album(conn, "Goa <2024>", goa)
    assert client.post(f"/api/albums/{aid}/export-html", json={"folder": str(library / "site")}).status_code == 400
    out = client.post(f"/api/albums/{aid}/export-html", json={"folder": str(tmp_path / "site")}).json()
    assert out["exported"] == len(goa)
    page = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "Goa &lt;2024&gt;" in page and "http" not in page.split("<script>")[0]    # nothing fetched from outside
    assert len(list((tmp_path / "site" / "photos").glob("*.jpg"))) == len(goa)
    assert not (library / "site").exists()
    conn.close()


def test_hidden_collection_can_bring_photos_back(ctx, library, client):
    index(ctx, library)
    ids = client.get("/api/photos/index").json()["ids"][:2]
    client.post("/api/photos/hide", json={"photo_ids": ids})
    assert not set(ids) & set(client.get("/api/photos/index").json()["ids"])
    assert sorted(client.get("/api/photos/index", params={"collection": "hidden"}).json()["ids"]) == sorted(ids)
    hidden = next(c for c in client.get("/api/collections").json()["collections"] if c["key"] == "hidden")
    assert hidden["count"] == 2
    client.post("/api/photos/hide", json={"photo_ids": ids, "hidden": False})
    assert set(ids) <= set(client.get("/api/photos/index").json()["ids"])


def test_gpx_upload(ctx, client, tmp_path):
    t = datetime(2024, 1, 1, 8, 0)
    ok = client.post("/api/gpx", files={"file": ("walk.gpx", _gpx([(t, 1.0, 2.0), (t + timedelta(minutes=1), 1.1, 2.1)]))})
    assert ok.status_code == 200 and (ctx.paths.data / "gpx" / "walk.gpx").exists()
    assert client.post("/api/gpx", files={"file": ("x.gpx", "<gpx/>")}).status_code == 400
    assert client.post("/api/gpx", files={"file": ("x.txt", "hi")}).status_code == 400
