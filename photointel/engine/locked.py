"""The Locked folder: photos only shown after a PIN, in this browser, for a few minutes.

A locked photo's status is 'locked', so every view that lists visible photos ('ok') leaves
it out — the timeline, search, people, places, map, albums, share links, memories,
exports — and every endpoint that serves its pixels or its faces refuses it unless the
Locked folder is open (see api/app.py). The file itself stays where it is on disk:
this hides a photo from people using Memoria, it does not encrypt it.
"""
from __future__ import annotations

import sqlite3

from .. import db
from . import visibility


def lock(conn: sqlite3.Connection, photo_ids: list[int]) -> int:
    ids = visibility.companions(conn, photo_ids)
    n = 0
    for chunk, marks in db.chunks(ids):
        # Not a photo private to someone: the route above already keeps those out, but a private photo
        # locked here would sit with locked=1 and status still 'private' until shared again — then
        # private.share()'s own CASE honours that stale flag and brings it back as 'locked', not 'ok'.
        conn.execute(f"UPDATE photos SET locked = 1 WHERE id IN ({marks}) AND private_to IS NULL", chunk)
        n += conn.execute(f"UPDATE photos SET status = 'locked' WHERE id IN ({marks}) AND status = 'ok'", chunk).rowcount
    db.audit(conn, "photos_locked", "photo", None, {"count": len(ids)})   # ids stay out of the log on purpose
    conn.commit()
    visibility.refresh(conn, ids)
    return n


def unlock(conn: sqlite3.Connection, photo_ids: list[int]) -> int:
    ids = visibility.companions(conn, photo_ids)
    n = 0
    for chunk, marks in db.chunks(ids):
        conn.execute(f"UPDATE photos SET locked = 0 WHERE id IN ({marks})", chunk)
        n += conn.execute(f"UPDATE photos SET status = 'ok' WHERE id IN ({marks}) AND status = 'locked'", chunk).rowcount
    db.audit(conn, "photos_unlocked", "photo", None, {"count": len(ids)})
    conn.commit()
    visibility.refresh(conn, ids)
    return n


def is_locked(conn: sqlite3.Connection, photo_id: int) -> bool:
    row = conn.execute("SELECT locked, status FROM photos WHERE id = ?", (photo_id,)).fetchone()
    return bool(row and (row["locked"] or row["status"] == "locked"))
