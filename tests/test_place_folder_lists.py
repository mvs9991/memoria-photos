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
