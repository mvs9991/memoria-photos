"""An event's cover, location confidence and people come from all of its photos, not its first 900."""
from __future__ import annotations

import time

from photointel import db
from photointel.engine import events


def _library(tmp_path, n):
    conn = db.init_db(tmp_path / "library.db")
    now = time.time()
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES (1, 'X:/photos', ?)", (now,))
    conn.executemany(
        "INSERT INTO photos(id, root_id, rel_path, folder, filename, ext, size, mtime, first_seen_at, last_seen_at, "
        "status, taken_ts, quality_score, location_confidence) VALUES (?,1,?,'',?,'.jpg',1,0,?,?,'ok',?,40,'low')",
        [(i, f"{i}.jpg", f"{i}.jpg", now, now, 1_600_000_000 + i) for i in range(1, n + 1)])
    conn.commit()
    return conn


def test_the_best_photo_late_in_a_big_event_is_its_cover(tmp_path):
    conn = _library(tmp_path, 2000)
    conn.execute("UPDATE photos SET quality_score = 95, location_confidence = 'high' WHERE id = 1500")
    ids = list(range(1, 2001))
    assert events._pick_cover(conn, ids) == 1500           # was the best of the first 900
    assert events._location_confidence(conn, ids) == "high"


def test_a_cover_tie_goes_to_the_lowest_id(tmp_path):
    conn = _library(tmp_path, 1000)
    assert events._pick_cover(conn, list(range(1000, 0, -1))) == 1


def test_an_event_summary_counts_everyone_else(tmp_path):
    import numpy as np

    from photointel.engine.people import create_person

    conn = _library(tmp_path, 10)
    model = db.register_model(conn, "face", "test-face", "1", 4, {})
    conn.execute("INSERT INTO events(id, kind, auto_title, start_ts, end_ts, photo_count, created_at, updated_at) "
                 "VALUES (1, 'event', 'Party', 1600000001, 1600000010, 10, 0, 0)")
    conn.execute("UPDATE photos SET event_id = 1")
    for k in range(1, 11):                     # ten people, one photo each
        pid = create_person(conn, f"P{k}", face_model=model)
        conn.execute("INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality, embedding, "
                     "person_id, created_at) VALUES (?,?,0.1,0.1,0.4,0.4,0.99,120,0.9,?,?,0)",
                     (k, model, np.ones(4, np.float16).tobytes(), pid))
    assert "and 7 others" in events.build_summary(conn, 1)   # was "and 1 other"
