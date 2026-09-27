"""Private photos: a family member's own photos, seen only by them.

With accounts on, each person can keep what their phone backs up to themselves. A private
photo's status is 'private' and `private_to` names its person, so — like the Locked folder —
every view that lists visible photos ('ok') leaves it out for everyone: timeline, search,
people, places, map, memories, albums, share links, exports. Its person sees it on their
Private page, and its pixels and details are refused to anyone else (deps.guard_locked).
It is also left out of the shared people, events and search index, for its person too:
those are built once for the whole family, and a private face must not become someone's cover.

Whose a file is follows the upload folder, `<upload root>/<name>/…`, where uploads from the
app and from backup apps are filed — not the uploads log, which is keyed by content and so
names only the last person who sent those bytes. With "keep my phone's photos private" on, new
files arriving in that person's folder are claimed right after scanning, before analysis, so
they are never visible, even for a moment. Older uploads change only when the person asks.

The files stay where they are: this hides photos from people using Memoria, and the owner of
the computer can still open the folder on disk.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import db
from . import visibility
from .uploads import _safe, upload_root


def _folder_prefix(username: str) -> str:
    return _safe(username).lower() + "/"


def _upload_root_id(ctx, conn: sqlite3.Connection) -> int | None:
    root = str(upload_root(ctx))
    for rid, path in conn.execute("SELECT id, path FROM roots"):
        if str(Path(path).resolve()).lower() == root.lower():
            return int(rid)
    return None


def claim_new(ctx, conn: sqlite3.Connection, root_id: int) -> int:
    """Just scanned: new files in the folder of someone keeping their uploads private become theirs."""
    if root_id != _upload_root_id(ctx, conn):
        return 0
    n = 0
    for uid, name in conn.execute("SELECT id, username FROM users WHERE private_uploads = 1 AND disabled = 0").fetchall():
        n += conn.execute(
            "UPDATE photos SET private_to = ? WHERE root_id = ? AND private_to IS NULL AND sha256 IS NULL "
            "AND meta_version IS NULL AND status = 'pending' AND lower(rel_path) LIKE ? ESCAPE '\\'",
            (uid, root_id, _like(_folder_prefix(name)))).rowcount
    if n:
        conn.commit()
    return n


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _mine_in_folder(ctx, conn: sqlite3.Connection, user: dict, photo_ids: list[int] | None) -> list[int]:
    """Visible photos in this person's upload folder (all of them, or those among `photo_ids`)."""
    rid = _upload_root_id(ctx, conn)
    if rid is None:
        return []
    sql = ("SELECT id FROM photos WHERE root_id = ? AND status = 'ok' AND private_to IS NULL "
           "AND lower(rel_path) LIKE ? ESCAPE '\\'")
    args: list = [rid, _like(_folder_prefix(user["username"]))]
    rows = [int(r[0]) for r in conn.execute(sql, args)]
    if photo_ids is None:
        return rows
    wanted = {int(p) for p in photo_ids}
    return [p for p in rows if p in wanted]


def make_private(ctx, conn: sqlite3.Connection, user: dict, photo_ids: list[int] | None) -> dict:
    """Only photos this person uploaded (their upload folder) can be made theirs alone.
    `photo_ids=None`: all of them — "make my earlier uploads private"."""
    mine = _mine_in_folder(ctx, conn, user, photo_ids)
    ids = visibility.companions(conn, mine) if mine else []
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE photos SET private_to = ?, status = CASE WHEN status IN ('ok', 'locked') "
                     f"THEN 'private' ELSE status END WHERE id IN ({marks})", (int(user["id"]), *ids))
        db.audit(conn, "photos_private", "user", int(user["id"]), {"count": len(ids)})   # no ids in the log
        conn.commit()
        visibility.refresh(conn, ids)
    skipped = 0 if photo_ids is None else len(set(photo_ids)) - len(mine)
    return {"private": len(mine), "not_yours": max(skipped, 0)}


def share(conn: sqlite3.Connection, user: dict, photo_ids: list[int]) -> int:
    """Back to the family: only this person's own private photos."""
    if not photo_ids:
        return 0
    marks = ",".join("?" * len(photo_ids))
    mine = [int(r[0]) for r in conn.execute(
        f"SELECT id FROM photos WHERE private_to = ? AND id IN ({marks})", (int(user["id"]), *photo_ids))]
    ids = visibility.companions(conn, mine) if mine else []
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    conn.execute(f"UPDATE photos SET private_to = NULL, status = CASE WHEN status = 'private' THEN "
                 f"(CASE WHEN locked = 1 THEN 'locked' ELSE 'ok' END) ELSE status END "
                 f"WHERE id IN ({marks}) AND private_to = ?", (*ids, int(user["id"])))
    db.audit(conn, "photos_shared", "user", int(user["id"]), {"count": len(ids)})
    conn.commit()
    visibility.refresh(conn, ids)
    return len(mine)


def count(conn: sqlite3.Connection, uid: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM photos WHERE private_to = ? AND status = 'private' "
                        "AND live_component = 0", (uid,)).fetchone()[0]
