"""What depends on which photos are visible, refreshed when the Trash, the Locked folder
or the Archive moves photos in or out of view."""
from __future__ import annotations

import logging
import sqlite3

from .. import db

log = logging.getLogger(__name__)


def companions(conn: sqlite3.Connection, photo_ids: list[int]) -> list[int]:
    """The video half of a Live photo goes wherever its still goes."""
    out = list(dict.fromkeys(int(p) for p in photo_ids))
    for i in range(0, len(out), 900):
        chunk = out[i:i + 900]
        for (vid,) in conn.execute(
                f"SELECT live_video_id FROM photos WHERE id IN ({','.join('?' * len(chunk))}) "
                "AND live_video_id IS NOT NULL", chunk):
            if vid not in out:
                out.append(int(vid))
    return out


def refresh(conn: sqlite3.Connection, photo_ids: list[int]) -> None:
    """Person counts and covers, and the search index's view of visible photos. The change
    itself is already committed; a failure here only leaves counts stale until the next index."""
    from .people import update_person_stats
    from .stacks import reassign_covers

    try:
        reassign_covers(conn, photo_ids)
        people: set[int] = set()
        for i in range(0, len(photo_ids), 900):
            chunk = photo_ids[i:i + 900]
            people |= {int(r[0]) for r in conn.execute(
                f"SELECT DISTINCT person_id FROM faces WHERE person_id IS NOT NULL AND photo_id IN "
                f"({','.join('?' * len(chunk))})", chunk)}
        if people:
            update_person_stats(conn, sorted(people))
        db.bump_generation(conn, "embeddings")      # the search matrix holds only 'ok' photos
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        log.exception("Refreshing counts failed; they will be rebuilt on the next index")
