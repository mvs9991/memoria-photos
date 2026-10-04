"""A place lists all its events, and a folder's count is what opening the folder shows.

The place page took the newest 100 events at a place (a real library's busiest place had 135), and the
folder list counted hidden photos and the video halves of live photos, which the folder's grid leaves out.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer


@pytest.fixture
def client(ctx, library):
    from photointel.api.app import create_app

    Indexer(ctx, workers=2).run(roots=[str(library)])
    return TestClient(create_app(ctx))


def test_a_place_lists_every_event_there(ctx, client):
    conn = ctx.connect()
    conn.execute("INSERT INTO places(id, name, kind, lat, lon, country_code) VALUES (501, 'Ongole', 'city', 15.5, 80.0, 'IN')")
    conn.execute("UPDATE photos SET place_id = 501 WHERE id = (SELECT MIN(id) FROM photos WHERE status = 'ok')")
    now = time.time()
    for i in range(130):
        conn.execute("INSERT INTO events(kind, start_ts, end_ts, photo_count, people_count, auto_title, place_id, created_at, updated_at) "
                     "VALUES ('event', ?, ?, 2, 0, ?, 501, ?, ?)", (now - i * 86400, now - i * 86400 + 60, f"E{i}", now, now))
    conn.commit()
    conn.close()
    assert len(client.get("/api/places/501").json()["events"]) == 130


def test_a_folder_count_leaves_out_what_its_grid_leaves_out(ctx, client):
    conn = ctx.connect()
    folder = conn.execute("SELECT folder FROM photos WHERE status = 'ok' GROUP BY folder ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    one = conn.execute("SELECT id FROM photos WHERE folder = ? AND status = 'ok' LIMIT 1", (folder,)).fetchone()[0]
    conn.execute("UPDATE photos SET hidden = 1 WHERE id = ?", (one,))
    conn.commit()
    conn.close()
    listed = {f["path"]: f["count"] for f in client.get("/api/folders").json()["folders"]}
    grid = client.get("/api/photos/index", params={"folder": folder, "folder_exact": True}).json()
    assert listed[folder] == grid["total"], f"the list says {listed[folder]}, the folder shows {grid['total']}"


def test_a_neighbourhood_shows_its_own_photos_and_a_city_includes_its_neighbourhoods(ctx, client):
    """Opening a neighbourhood used to show its whole city: the place filter widened by "same city", so
    Guddalaguntapalem (in Ongole) listed 4,832 photos and opened to all 6,545 of Ongole's."""
    conn = ctx.connect()
    conn.execute("INSERT INTO places(id, name, city, kind, lat, lon, country_code) VALUES (601, 'Ongole', 'Ongole', 'city', 15.5, 80.0, 'IN')")
    conn.execute("INSERT INTO places(id, name, city, kind, lat, lon, country_code) VALUES (602, 'Guddalaguntapalem', 'Ongole', 'suburb', 15.51, 80.01, 'IN')")
    conn.execute("INSERT INTO places(id, name, city, kind, lat, lon, country_code) VALUES (603, 'Kothapatnam', 'Ongole', 'suburb', 15.45, 80.10, 'IN')")
    ids = [r[0] for r in conn.execute("SELECT id FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 ORDER BY id LIMIT 6")]
    for pid, place in zip(ids, (601, 602, 602, 603, 603, 603)):
        conn.execute("UPDATE photos SET place_id = ? WHERE id = ?", (place, pid))
    conn.commit()
    conn.close()
    total = lambda place: client.get(f"/api/photos/index?place={place}").json()["total"]  # noqa: E731
    assert total(602) == 2, "a neighbourhood opened to more than its own photos"
    assert total(603) == 3
    assert total(601) == 6, "a city should still include its neighbourhoods"


def test_the_places_list_neither_counts_nor_shows_a_hidden_photo(ctx, client):
    conn = ctx.connect()
    conn.execute("INSERT INTO places(id, name, city, kind, lat, lon, country_code) VALUES (701, 'Goa', 'Goa', 'city', 15.5, 73.8, 'IN')")
    ids = [r[0] for r in conn.execute("SELECT id FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 ORDER BY id LIMIT 3")]
    conn.execute(f"UPDATE photos SET place_id = 701 WHERE id IN ({','.join(map(str, ids))})")
    conn.execute("UPDATE photos SET quality_score = 0.99, hidden = 1 WHERE id = ?", (ids[0],))   # the best photo is hidden
    conn.commit()
    conn.close()
    goa = next(p for p in client.get("/api/places").json()["places"] if p["id"] == 701)
    assert goa["photo_count"] == 2 == client.get("/api/photos/index?place=701").json()["total"]
    assert goa["cover_photo_id"] != ids[0], "a hidden photo is the place's cover"
