"""The headline numbers describe what you can see.

/api/stats feeds the sidebar ("Photos 29,500") and the home page. It counted hidden photos, so after
a phone's and Google's trash were hidden the sidebar still claimed 29,500 photos while every page
listed fewer. Disk usage is the one number that should keep counting them: the files are still on
the disk whether or not they are shown.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages


@pytest.fixture
def served(ctx, library):
    from photointel.api.app import create_app

    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    return ctx, conn, TestClient(create_app(ctx), raise_server_exceptions=False)


def stats(client):
    return client.get("/api/stats").json()


def hide(conn, where):
    n = conn.execute(f"UPDATE photos SET hidden = 1 WHERE {where}").rowcount
    conn.commit()
    return n


def test_hiding_photos_lowers_the_photo_count(served):
    ctx, conn, client = served
    before = stats(client)["photos"]
    assert hide(conn, "rel_path LIKE 'Trips/Goa/IMG_x%'") == 5
    assert stats(client)["photos"] == before - 5


def test_hidden_photos_leave_the_gps_count_and_the_places(served):
    ctx, conn, client = served
    # The test library has no offline place data, so give it two places by hand.
    conn.execute("INSERT INTO places(id, name, kind, lat, lon, country_code) VALUES (101, 'Hyderabad', 'city', 17.38, 78.48, 'IN')")
    conn.execute("INSERT INTO places(id, name, kind, lat, lon, country_code) VALUES (102, 'Goa', 'city', 15.54, 73.75, 'IN')")
    conn.execute("UPDATE photos SET place_id = 101 WHERE rel_path LIKE 'DCIM/%'")
    conn.execute("UPDATE photos SET place_id = 102 WHERE rel_path LIKE 'Trips/Goa/%'")
    conn.commit()
    before = stats(client)
    assert before["places"] == 2
    hide(conn, "rel_path LIKE 'Trips/Goa/%'")
    after = stats(client)
    assert after["with_gps"] == before["with_gps"] - 5
    assert after["places"] == 1, "a place whose every photo is hidden should not be counted"
    assert [p["name"] for p in after["top_places"]] == ["Hyderabad"]


def test_the_date_range_ignores_hidden_photos(served):
    ctx, conn, client = served
    before = stats(client)["date_range"]
    # The July (Goa) photos are the newest dated ones in the fixture library.
    hide(conn, "rel_path LIKE 'Trips/Goa/%'")
    after = stats(client)["date_range"]
    assert after["to"] < before["to"], "the newest photo is hidden, so it must not set the end of the range"
    assert after["from"] == before["from"]


def test_hidden_videos_are_not_counted(served):
    ctx, conn, client = served
    conn.execute("UPDATE photos SET media_type = 'video' WHERE rel_path = 'Trips/Goa/IMG_x2.jpg'")
    conn.commit()
    with_video = stats(client)["videos"]
    assert with_video == 1
    hide(conn, "rel_path = 'Trips/Goa/IMG_x2.jpg'")
    assert stats(client)["videos"] == 0


def test_disk_usage_still_counts_hidden_files(served):
    """Hiding removes a photo from the views, not from the disk."""
    ctx, conn, client = served
    before = stats(client)["bytes"]
    hide(conn, "rel_path LIKE 'Trips/Goa/%'")
    assert stats(client)["bytes"] == before > 0


def test_nothing_hidden_means_nothing_changes(served):
    ctx, conn, client = served
    s = stats(client)
    visible = conn.execute("SELECT COUNT(*) FROM photos WHERE status='ok' AND hidden=0 AND live_component=0").fetchone()[0]
    assert s["photos"] == visible
