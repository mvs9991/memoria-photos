"""The Events page lists every event, not the newest 500.

/api/events defaulted to LIMIT 500. A real library had 734 events and 18 trips, so the page silently
showed only the newest 500: every older event, and 4 of the trips, never appeared.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(ctx):
    from photointel.api.app import create_app

    return TestClient(create_app(ctx))


def test_more_than_five_hundred_events_are_all_listed(ctx, client):
    conn = ctx.connect()
    now = time.time()
    for i in range(620):
        conn.execute("INSERT INTO events(kind, start_ts, end_ts, photo_count, people_count, auto_title, created_at, updated_at) "
                     "VALUES ('event', ?, ?, 3, 0, ?, ?, ?)", (now - i * 86400, now - i * 86400 + 3600, f"Day {i}", now, now))
    for i in range(7):
        conn.execute("INSERT INTO events(kind, start_ts, end_ts, photo_count, people_count, auto_title, created_at, updated_at) "
                     "VALUES ('trip', ?, ?, 9, 0, ?, ?, ?)", (now - (600 + i) * 86400, now - (598 + i) * 86400, f"Trip {i}", now, now))
    conn.commit()
    conn.close()
    events = client.get("/api/events").json()["events"]
    assert len(events) == 627, f"only {len(events)} of 627 events were listed"
    assert sum(e["kind"] == "trip" for e in events) == 7
