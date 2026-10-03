"""Hidden photos must not feed the things built from the photos you can see.

On a real library, after 124 photos were hidden (a phone's and Google's trash): 24 events had no
visible photo left (empty ghosts in the Events list), 10 used a hidden photo as their cover and 36
showed the wrong count; 5 people were listed with a count but no visible photo and 5 used a hidden
photo's face as their cover. Event detection and the people statistics only ever filtered on
status, never on hidden, so hiding changed nothing downstream.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel import db
from photointel.engine import events as events_mod
from photointel.engine.people import update_person_stats
from photointel.pipeline import jobs
from photointel.pipeline import post as post_mod
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.jobs import _nothing_changed
from photointel.pipeline.post import run_post_stages

VIS = "status = 'ok' AND hidden = 0 AND live_component = 0"


@pytest.fixture
def built(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    return ctx, conn, library


def hide(conn, where: str, *args) -> int:
    n = conn.execute(f"UPDATE photos SET hidden = 1 WHERE {where}", args).rowcount
    conn.commit()
    return n


def goa_event(conn):
    return conn.execute(
        "SELECT DISTINCT event_id FROM photos WHERE rel_path LIKE 'Trips/Goa/%' AND event_id IS NOT NULL").fetchone()[0]


# ------------------------------------------------------------------------- events

def test_an_event_whose_photos_are_all_hidden_disappears(built):
    ctx, conn, _ = built
    before = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'event'").fetchone()[0]
    assert hide(conn, "rel_path LIKE 'Trips/Goa/%'") == 5
    events_mod.detect_events(ctx, conn)
    after = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'event'").fetchone()[0]
    assert after == before - 1, "the Goa event should be gone, not left as an empty ghost"
    ghosts = conn.execute(
        f"SELECT COUNT(*) FROM events e WHERE e.kind = 'event' AND NOT EXISTS "
        f"(SELECT 1 FROM photos p WHERE p.event_id = e.id AND p.{VIS.replace(' AND ', ' AND p.')})").fetchone()[0]
    assert ghosts == 0


def test_no_hidden_photo_stays_assigned_to_an_event(built):
    ctx, conn, _ = built
    hide(conn, "rel_path LIKE 'DCIM/Camera/IMG_2024030%'")
    events_mod.detect_events(ctx, conn)
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE hidden = 1 AND event_id IS NOT NULL").fetchone()[0] == 0


def test_an_events_cover_is_never_a_hidden_photo(built):
    ctx, conn, _ = built
    eid = goa_event(conn)
    cover = conn.execute("SELECT cover_photo_id FROM events WHERE id = ?", (eid,)).fetchone()[0]
    assert cover is not None
    hide(conn, "id = ?", cover)
    events_mod.detect_events(ctx, conn)
    covers = conn.execute(
        "SELECT e.id, e.cover_photo_id FROM events e JOIN photos p ON p.id = e.cover_photo_id WHERE p.hidden = 1").fetchall()
    assert covers == [], f"events still use a hidden photo as their cover: {[tuple(c) for c in covers]}"


def test_event_counts_match_what_is_visible(built):
    ctx, conn, _ = built
    hide(conn, "rel_path IN ('Trips/Goa/IMG_x0.jpg', 'Trips/Goa/IMG_x1.jpg')")
    events_mod.detect_events(ctx, conn)
    bad = conn.execute(
        f"SELECT COUNT(*) FROM events e WHERE e.kind = 'event' AND e.photo_count != "
        f"(SELECT COUNT(*) FROM photos p WHERE p.event_id = e.id AND p.{VIS.replace(' AND ', ' AND p.')})").fetchone()[0]
    assert bad == 0


def test_showing_the_photos_again_brings_the_event_back(built):
    ctx, conn, _ = built
    before = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'event'").fetchone()[0]
    hide(conn, "rel_path LIKE 'Trips/Goa/%'")
    events_mod.detect_events(ctx, conn)
    conn.execute("UPDATE photos SET hidden = 0 WHERE rel_path LIKE 'Trips/Goa/%'")
    conn.commit()
    events_mod.detect_events(ctx, conn)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'event'").fetchone()[0] == before


# ------------------------------------------------------------------------- people

def top_person(conn) -> int:
    return conn.execute("SELECT id FROM persons WHERE face_count > 0 AND merged_into IS NULL "
                        "ORDER BY face_count DESC").fetchone()[0]


def visible_photos_of(conn, pid: int) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT f.photo_id) FROM faces f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.person_id = ? AND p.status = 'ok' AND p.hidden = 0", (pid,)).fetchone()[0]


def test_hiding_photos_lowers_a_persons_count(built):
    ctx, conn, _ = built
    pid = top_person(conn)
    total = conn.execute("SELECT photo_count FROM persons WHERE id = ?", (pid,)).fetchone()[0]
    assert total >= 2
    victim = conn.execute("SELECT photo_id FROM faces WHERE person_id = ? LIMIT 1", (pid,)).fetchone()[0]
    hide(conn, "id = ?", victim)
    update_person_stats(conn, [pid])
    conn.commit()
    now = conn.execute("SELECT photo_count FROM persons WHERE id = ?", (pid,)).fetchone()[0]
    assert now == visible_photos_of(conn, pid) == total - 1


def test_a_persons_cover_face_is_never_from_a_hidden_photo(built):
    ctx, conn, _ = built
    pid = top_person(conn)
    cover_photo = conn.execute("SELECT f.photo_id FROM persons p JOIN faces f ON f.id = p.cover_face_id "
                               "WHERE p.id = ?", (pid,)).fetchone()[0]
    hide(conn, "id = ?", cover_photo)
    update_person_stats(conn, [pid])
    conn.commit()
    row = conn.execute("SELECT ph.hidden FROM persons p JOIN faces f ON f.id = p.cover_face_id "
                       "JOIN photos ph ON ph.id = f.photo_id WHERE p.id = ?", (pid,)).fetchone()
    assert row is None or row[0] == 0


def test_a_person_with_no_visible_photo_has_a_zero_count(built):
    ctx, conn, _ = built
    pid = top_person(conn)
    conn.execute("UPDATE photos SET hidden = 1 WHERE id IN (SELECT photo_id FROM faces WHERE person_id = ?)", (pid,))
    conn.commit()
    update_person_stats(conn, [pid])
    conn.commit()
    assert conn.execute("SELECT photo_count FROM persons WHERE id = ?", (pid,)).fetchone()[0] == 0


def test_hiding_through_the_api_updates_the_count_straight_away(built):
    """visibility.refresh is what the Hide button calls; it used to leave the count alone."""
    from photointel.api.app import create_app

    ctx, conn, _ = built
    pid = top_person(conn)
    total = conn.execute("SELECT photo_count FROM persons WHERE id = ?", (pid,)).fetchone()[0]
    victim = conn.execute("SELECT photo_id FROM faces WHERE person_id = ? LIMIT 1", (pid,)).fetchone()[0]
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    assert client.post("/api/photos/hide", json={"photo_ids": [victim]}).json()["changed"] == 1
    now = ctx.connect().execute("SELECT photo_count FROM persons WHERE id = ?", (pid,)).fetchone()[0]
    assert now == total - 1


# ------------------------------------------------------------- the scheduled run must not skip it

def test_hiding_marks_derived_data_stale_so_a_scheduled_run_does_not_skip(built, monkeypatch):
    """Hiding changes no file, so the scan cannot tell; without this flag the new scheduled run
    would call the library "unchanged" and never rebuild events or duplicates."""
    from photointel.api.app import create_app

    ctx, conn, library = built
    calls: list = []
    real = post_mod.run_post_stages

    def spy(c, cn, stages=None, **kw):
        calls.append(stages)
        return real(c, cn, stages=stages, **kw)

    monkeypatch.setattr(post_mod, "run_post_stages", spy)
    jobs.run_index_job(ctx, roots=[str(library)], workers=2)           # a clean, finished full run
    assert db.get_meta(ctx.connect(), "post_pending") == "0"
    skipped = jobs.run_index_job(ctx, roots=[str(library)], workers=2, only_if_changed=True)
    assert skipped["post"] == {"skipped": "nothing changed"}

    victim = conn.execute("SELECT id FROM photos WHERE rel_path LIKE 'Trips/Goa/%' LIMIT 1").fetchone()[0]
    TestClient(create_app(ctx), raise_server_exceptions=False).post("/api/photos/hide", json={"photo_ids": [victim]})
    assert db.get_meta(ctx.connect(), "post_pending") == "1"

    out = jobs.run_index_job(ctx, roots=[str(library)], workers=2, only_if_changed=True)
    assert "skipped" not in out["post"], "a hide must make the next scheduled run rebuild"
    assert db.get_meta(ctx.connect(), "post_pending") == "0"
