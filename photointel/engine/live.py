"""Live photos and motion photos.

* iPhone Live photos (and Takeout exports of them) are two files: a still and a short
  video with the same name in the same folder. They are paired so the library shows one
  item that can play, and the video half is kept out of grids, search, events, face
  clustering and duplicate detection (`photos.live_component = 1`).
* Google/Samsung motion photos carry the video inside the JPEG. The indexer records its
  offset; `backfill_motion_offsets` does the same for JPEGs indexed before that existed.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

from .. import db, video

log = logging.getLogger(__name__)

PAIR_MAX_CLOCK_SKEW_S = 120     # a still and its motion are recorded together
_HEAD_BYTES = 256 * 1024


def pair_live_photos(conn: sqlite3.Connection) -> dict:
    t0 = time.time()
    before = {int(r[0]) for r in conn.execute("SELECT id FROM photos WHERE live_component = 1")}
    stills: dict[tuple, list] = {}
    for r in conn.execute(
            "SELECT id, root_id, folder, filename, taken_ts FROM photos "
            "WHERE media_type = 'image' AND status = 'ok'"):
        key = (r["root_id"], r["folder"], os.path.splitext(r["filename"])[0].lower())
        stills.setdefault(key, []).append(r)

    pairs: list[tuple[int, int]] = []
    for v in conn.execute(
            "SELECT id, root_id, folder, filename, taken_ts, duration FROM photos "
            "WHERE media_type = 'video' AND status = 'ok' "
            "AND (duration IS NULL OR duration <= ?)", (video.LIVE_MAX_SECONDS,)):
        cands = stills.get((v["root_id"], v["folder"], os.path.splitext(v["filename"])[0].lower()))
        if not cands:
            continue
        still = cands[0]
        if (still["taken_ts"] is not None and v["taken_ts"] is not None
                and abs(still["taken_ts"] - v["taken_ts"]) > PAIR_MAX_CLOCK_SKEW_S):
            continue  # same name, different moment (a reused IMG_0001 on another day)
        pairs.append((int(still["id"]), int(v["id"])))

    conn.execute("UPDATE photos SET live_video_id = NULL WHERE live_video_id IS NOT NULL")
    conn.execute("UPDATE photos SET live_component = 0 WHERE live_component = 1")
    conn.executemany("UPDATE photos SET live_video_id = ? WHERE id = ?", [(vid, sid) for sid, vid in pairs])
    conn.executemany("UPDATE photos SET live_component = 1 WHERE id = ?", [(vid,) for _, vid in pairs])
    after = {vid for _, vid in pairs}
    if after != before:
        # Search, "similar" and clustering read only non-component photos.
        db.bump_generation(conn, "embeddings")
    conn.commit()
    out = {"live_pairs": len(pairs), "seconds": round(time.time() - t0, 2)}
    log.info("Live photos: %s", out)
    return out


def backfill_motion_offsets(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    """Look for embedded motion video in JPEGs indexed before detection existed.

    Reads only the first 256 KB of each file unless its XMP declares a motion photo.
    Older Samsung files that mark the video *only* in a trailer are not found this way;
    they are detected when a file is (re)indexed, which reads the whole file anyway.
    """
    rows = conn.execute(
        "SELECT p.id, r.path AS root, p.rel_path FROM photos p JOIN roots r ON r.id = p.root_id "
        "WHERE p.status = 'ok' AND p.media_type = 'image' AND p.format = 'JPEG' AND p.motion_offset IS NULL"
        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    found = checked = 0
    updates = []
    for r in rows:
        path = os.path.join(r["root"], r["rel_path"])
        offset = 0
        try:
            with open(path, "rb") as f:
                head = f.read(_HEAD_BYTES)
                if any(h in head for h in (b"MicroVideo", b"MotionPhoto", b"Container:Directory")):
                    f.seek(0)
                    offset = video.find_motion_offset(f.read()) or 0
        except OSError:
            continue  # unreadable right now (unmounted?): leave NULL to try again next run
        checked += 1
        found += 1 if offset else 0
        updates.append((offset, int(r["id"])))
    conn.executemany("UPDATE photos SET motion_offset = ? WHERE id = ?", updates)
    conn.commit()
    return {"checked": checked, "motion_photos": found}
