"""Date and location corrections made by the user.

A wrong camera clock or a missing GPS fix is corrected in Memoria's database only; the
original file is never written. Corrections live in `photo_overrides`, apart from the
values read from the file, because re-indexing a changed file rewrites those — the
indexer calls `apply_overrides` after every write so a correction always wins.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta

from .. import db
from ..metadata import naive_to_ts, ts_to_naive

FMT = "%Y-%m-%d %H:%M:%S"


def set_date(conn: sqlite3.Connection, photo_ids: list[int], taken_local: str | None = None,
             shift_seconds: float | None = None) -> dict:
    """Set an absolute date/time, or shift each photo's current time (a camera clock set
    wrong by a fixed amount is the common case, and a shift keeps the burst order)."""
    if (taken_local is None) == (shift_seconds is None):
        raise ValueError("give exactly one of taken_local or shift_seconds")
    now = time.time()
    rows = [r for chunk, q in db.chunks(photo_ids)      # a selection can be bigger than SQLite binds at once
            for r in conn.execute(f"SELECT id, taken_ts FROM photos WHERE id IN ({q})", chunk).fetchall()]
    for r in rows:
        if taken_local is not None:
            dt = parse_local(taken_local)
        else:
            if r["taken_ts"] is None:
                continue
            dt = ts_to_naive(r["taken_ts"]) + timedelta(seconds=shift_seconds)
        conn.execute(
            """INSERT INTO photo_overrides(photo_id, taken_local, updated_at) VALUES (?,?,?)
               ON CONFLICT(photo_id) DO UPDATE SET taken_local = excluded.taken_local, updated_at = excluded.updated_at""",
            (r["id"], dt.strftime(FMT), now))
    applied = apply_overrides(conn, [r["id"] for r in rows])
    db.audit(conn, "date_corrected", "photo", None,
             {"photos": photo_ids[:2000], "set": taken_local, "shift_seconds": shift_seconds})
    conn.commit()
    return {"corrected": applied}


def parse_local(text: str) -> datetime:
    """'2024-03-09', '2024-03-09 18:30' or '2024-03-09T18:30:05' -> naive wall-clock datetime."""
    t = text.strip().replace("T", " ")
    for fmt in (FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(t[:19], fmt)
        except ValueError:
            continue
    raise ValueError(f"not a date: {text!r}")


def set_location(conn: sqlite3.Connection, photo_ids: list[int], lat: float, lon: float) -> dict:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude/longitude out of range")
    now = time.time()
    conn.executemany(
        """INSERT INTO photo_overrides(photo_id, lat, lon, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(photo_id) DO UPDATE SET lat = excluded.lat, lon = excluded.lon,
               updated_at = excluded.updated_at""",
        [(int(p), lat, lon, now) for p in photo_ids])
    applied = apply_overrides(conn, photo_ids)
    db.audit(conn, "location_corrected", "photo", None, {"photos": photo_ids[:2000], "lat": lat, "lon": lon})
    conn.commit()
    return {"corrected": applied}


def clear(conn: sqlite3.Connection, photo_ids: list[int]) -> dict:
    """Forget corrections. The file's own values return on the next re-index of the photo."""
    n = 0
    for chunk, marks in db.chunks(photo_ids):
        n += conn.execute(f"DELETE FROM photo_overrides WHERE photo_id IN ({marks})", chunk).rowcount
        conn.execute(f"UPDATE photos SET meta_version = NULL WHERE id IN ({marks})", chunk)  # re-read the file
    db.audit(conn, "corrections_cleared", "photo", None, {"photos": photo_ids[:2000]})
    conn.commit()
    return {"cleared": n}


def apply_overrides(conn: sqlite3.Connection, photo_ids: list[int] | None = None) -> int:
    sql = "SELECT photo_id, taken_local, lat, lon FROM photo_overrides"
    if photo_ids is None:
        overrides = conn.execute(sql).fetchall()
    else:
        if not photo_ids:
            return 0
        overrides = [o for chunk, q in db.chunks(photo_ids)
                     for o in conn.execute(f"{sql} WHERE photo_id IN ({q})", chunk).fetchall()]
    n = 0
    for o in overrides:
        if o["taken_local"]:
            dt = datetime.strptime(o["taken_local"], FMT)
            conn.execute("UPDATE photos SET taken_ts = ?, taken_local = ?, date_source = 'user', "
                         "date_confidence = 'high' WHERE id = ?", (naive_to_ts(dt), o["taken_local"], o["photo_id"]))
        if o["lat"] is not None:
            conn.execute("UPDATE photos SET gps_lat = ?, gps_lon = ?, place_id = CASE WHEN "
                         "gps_lat IS ? AND gps_lon IS ? THEN place_id ELSE NULL END, "
                         "location_source = 'user', location_confidence = 'high' WHERE id = ?",
                         (o["lat"], o["lon"], o["lat"], o["lon"], o["photo_id"]))
        n += 1
    return n
