"""Albums and user tags: organisation the user decides, never the algorithm.

Album membership is stored per file, but *shown* per content: when an album holds a
file whose byte-identical copy lives elsewhere (Google Takeout puts every album photo
in the album folder *and* the year folder), the album shows whichever copy is visible.
Hiding a duplicate copy therefore never empties an album.
"""
from __future__ import annotations

import sqlite3
import time

from .. import db

VISIBLE = "p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0"
USER_TAG_SCORE = 10.0      # far above every threshold: a tag the user set always applies


# ---------------------------------------------------------------------------- albums

def visible_sql(alias: str, user_id: int | None) -> str:
    """SQL true for albums `user_id` may see: everything shared, and their own private ones."""
    if user_id is None:
        return "1"
    return f"({alias}.private = 0 OR {alias}.owner_user_id = {int(user_id)})"


def can_see(conn: sqlite3.Connection, album_id: int, user_id: int | None):
    """The album row if it exists, is not deleted and this account may see it; else None."""
    return conn.execute(f"SELECT * FROM albums a WHERE a.id = ? AND a.hidden = 0 AND {visible_sql('a', user_id)}",
                        (album_id,)).fetchone()


def create_album(conn: sqlite3.Connection, name: str, photo_ids: list[int] | None = None,
                 description: str | None = None, source: str = "user", source_key: str | None = None,
                 owner_user_id: int | None = None) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO albums(name, description, source, source_key, created_at, updated_at, owner_user_id) "
        "VALUES (?,?,?,?,?,?,?)", (name.strip(), description, source, source_key, now, now, owner_user_id))
    aid = int(cur.lastrowid)
    if photo_ids:
        add_photos(conn, aid, photo_ids, commit=False)
    db.audit(conn, "album_created", "album", aid, {"name": name, "source": source},
             actor="user" if source == "user" else "import")
    db.bump_generation(conn, "albums")
    conn.commit()
    return aid


def rename_album(conn: sqlite3.Connection, album_id: int, name: str | None = None,
                 description: str | None = None) -> None:
    sets, args = ["updated_at = ?"], [time.time()]
    if name is not None and name.strip():
        sets.append("name = ?")
        args.append(name.strip())
    if description is not None:
        sets.append("description = ?")
        args.append(description.strip() or None)
    conn.execute(f"UPDATE albums SET {', '.join(sets)} WHERE id = ?", (*args, album_id))
    db.audit(conn, "album_updated", "album", album_id, {"name": name, "description": description})
    db.bump_generation(conn, "albums")
    conn.commit()


def delete_album(conn: sqlite3.Connection, album_id: int) -> None:
    """Deletes the album, never its photos. An imported album is only hidden, so the
    next index run does not bring it back."""
    row = conn.execute("SELECT source FROM albums WHERE id = ?", (album_id,)).fetchone()
    if row is None:
        return
    if row["source"] == "user":
        conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    else:
        conn.execute("UPDATE albums SET hidden = 1, updated_at = ? WHERE id = ?", (time.time(), album_id))
    db.audit(conn, "album_deleted", "album", album_id, {})
    db.bump_generation(conn, "albums")
    conn.commit()


def add_photos(conn: sqlite3.Connection, album_id: int, photo_ids: list[int], commit: bool = True) -> int:
    now = time.time()
    n = conn.executemany("INSERT OR IGNORE INTO album_photos(album_id, photo_id, added_at) VALUES (?,?,?)",
                         [(album_id, int(p), now) for p in photo_ids]).rowcount
    conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (now, album_id))
    if commit:
        db.audit(conn, "album_photos_added", "album", album_id, {"photos": list(photo_ids)[:2000]})
        conn.commit()
    return n


def remove_photos(conn: sqlite3.Connection, album_id: int, photo_ids: list[int]) -> int:
    """Removes content from the album: every copy sharing the photos' hashes goes."""
    marks = ",".join("?" * len(photo_ids))
    shas = [r[0] for r in conn.execute(
        f"SELECT sha256 FROM photos WHERE id IN ({marks}) AND sha256 IS NOT NULL", photo_ids)]
    n = conn.execute(f"DELETE FROM album_photos WHERE album_id = ? AND photo_id IN ({marks})",
                     (album_id, *photo_ids)).rowcount
    if shas:
        n += conn.execute(
            f"DELETE FROM album_photos WHERE album_id = ? AND photo_id IN "
            f"(SELECT id FROM photos WHERE sha256 IN ({','.join('?' * len(shas))}))", (album_id, *shas)).rowcount
    conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (time.time(), album_id))
    db.audit(conn, "album_photos_removed", "album", album_id, {"photos": list(photo_ids)[:2000]})
    conn.commit()
    return n


def album_photo_ids(conn: sqlite3.Connection, album_id: int) -> list[int]:
    """Visible photos of an album in capture order, one per distinct content."""
    members = conn.execute(
        """SELECT p.id, p.sha256, p.taken_ts, (p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0) AS vis
           FROM album_photos ap JOIN photos p ON p.id = ap.photo_id WHERE ap.album_id = ?""",
        (album_id,)).fetchall()
    need = [m["sha256"] for m in members if not m["vis"] and m["sha256"]]
    visible_copy: dict[str, tuple[int, float | None]] = {}
    for i in range(0, len(need), 900):
        chunk = need[i:i + 900]
        for r in conn.execute(
                f"SELECT p.id, p.sha256, p.taken_ts FROM photos p WHERE {VISIBLE} "
                f"AND p.sha256 IN ({','.join('?' * len(chunk))})", chunk):
            visible_copy.setdefault(r["sha256"], (int(r["id"]), r["taken_ts"]))
    seen: set = set()
    out: list[tuple[float, int]] = []
    for m in members:
        if m["vis"]:
            pid, ts = int(m["id"]), m["taken_ts"]
        elif m["sha256"] in visible_copy:
            pid, ts = visible_copy[m["sha256"]]
        else:
            continue
        key = m["sha256"] or ("id", pid)
        if key in seen:
            continue
        seen.add(key)
        out.append((ts or 0.0, pid))
    return [pid for _, pid in sorted(out)]


def create_smart_album(conn: sqlite3.Connection, name: str, query: str, owner_user_id: int | None = None) -> int:
    """An album that is a saved search: its contents follow the library as it changes."""
    aid = create_album(conn, name, owner_user_id=owner_user_id)
    conn.execute("UPDATE albums SET kind = 'smart', query = ? WHERE id = ?", (query.strip(), aid))
    conn.commit()
    return aid


def list_albums(conn: sqlite3.Connection, resolve=None, user_id: int | None = None) -> list[dict]:
    """`resolve(query) -> photo ids` runs a smart album's saved search (the API passes the
    search engine; without it smart albums are listed empty rather than guessed). With
    `user_id`, only the albums that account may see."""
    out = []
    for a in conn.execute(f"SELECT * FROM albums a WHERE a.hidden = 0 AND {visible_sql('a', user_id)} "
                          "ORDER BY a.updated_at DESC").fetchall():
        if a["kind"] == "smart":
            ids = list(resolve(a["query"])) if resolve else []
        else:
            ids = album_photo_ids(conn, a["id"])
        cover = a["cover_photo_id"] if a["cover_photo_id"] in ids else None
        if cover is None and ids:
            cover = _best_photo(conn, ids)
        out.append({"id": a["id"], "name": a["name"], "description": a["description"], "source": a["source"],
                    "kind": a["kind"], "query": a["query"],
                    "photo_count": len(ids), "cover_photo_id": cover,
                    "start_ts": _ts(conn, ids, "MIN"), "end_ts": _ts(conn, ids, "MAX"),
                    "updated_at": a["updated_at"], "private": bool(a["private"]),
                    "mine": user_id is not None and a["owner_user_id"] == user_id})
    return out


def albums_for_photo(conn: sqlite3.Connection, photo_id: int, user_id: int | None = None) -> list[dict]:
    rows = conn.execute(
        f"""SELECT DISTINCT a.id, a.name, a.source FROM albums a JOIN album_photos ap ON ap.album_id = a.id
           JOIN photos p ON p.id = ap.photo_id
           WHERE a.hidden = 0 AND {visible_sql('a', user_id)} AND (p.id = ? OR p.sha256 = (SELECT sha256 FROM photos WHERE id = ?))
           ORDER BY a.name""", (photo_id, photo_id)).fetchall()
    return [dict(r) for r in rows]


def _best_photo(conn, ids: list[int]) -> int | None:
    best: tuple[float, int] | None = None
    for i in range(0, len(ids), 900):          # every photo, 900 at a time (SQLite's variable limit)
        chunk = ids[i:i + 900]
        row = conn.execute(
            f"SELECT id, COALESCE(quality_score, 0) q FROM photos WHERE id IN ({','.join('?' * len(chunk))}) "
            f"ORDER BY q DESC LIMIT 1", chunk).fetchone()
        if row and (best is None or row[1] > best[0]):
            best = (row[1], int(row[0]))
    return best[1] if best else None


def _ts(conn, ids: list[int], fn: str) -> float | None:
    vals = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        v = conn.execute(f"SELECT {fn}(taken_ts) FROM photos WHERE id IN ({','.join('?' * len(chunk))})",
                         chunk).fetchone()[0]
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return min(vals) if fn == "MIN" else max(vals)


# ---------------------------------------------------------------------------- user tags

def _tag_id(conn: sqlite3.Connection, name: str, create: bool) -> int | None:
    name = " ".join(name.strip().lower().split())
    if not name:
        return None
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row[0])
    if not create:
        return None
    return int(conn.execute("INSERT INTO tags(name, category) VALUES (?, 'user')", (name,)).lastrowid)


def add_user_tag(conn: sqlite3.Connection, photo_ids: list[int], name: str) -> dict:
    tid = _tag_id(conn, name, create=True)
    if tid is None:
        return {"tagged": 0}
    conn.executemany(
        """INSERT INTO photo_tags(photo_id, tag_id, score, source) VALUES (?,?,?, 'user')
           ON CONFLICT(photo_id, tag_id) DO UPDATE SET score = excluded.score, source = 'user'""",
        [(int(p), tid, USER_TAG_SCORE) for p in photo_ids])
    db.audit(conn, "tag_added", "tag", tid, {"photos": list(photo_ids)[:2000]})
    db.bump_generation(conn, "tags")
    conn.commit()
    return {"tagged": len(photo_ids), "tag_id": tid}


def remove_user_tag(conn: sqlite3.Connection, photo_ids: list[int], name: str) -> dict:
    """Removing a tag is remembered (source 'user_removed', negative score), so a later
    automatic re-tagging cannot put back what the user took off."""
    tid = _tag_id(conn, name, create=False)
    if tid is None:
        return {"removed": 0}
    conn.executemany(
        """INSERT INTO photo_tags(photo_id, tag_id, score, source) VALUES (?,?, -1, 'user_removed')
           ON CONFLICT(photo_id, tag_id) DO UPDATE SET score = -1, source = 'user_removed'""",
        [(int(p), tid) for p in photo_ids])
    db.audit(conn, "tag_removed", "tag", tid, {"photos": list(photo_ids)[:2000]})
    db.bump_generation(conn, "tags")
    conn.commit()
    return {"removed": len(photo_ids)}


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT t.name, t.category, SUM(pt.source = 'user') AS by_user, COUNT(*) AS n
           FROM tags t JOIN photo_tags pt ON pt.tag_id = t.id
           JOIN photos p ON p.id = pt.photo_id
           WHERE pt.score >= 2.0 AND p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0
           GROUP BY t.id ORDER BY (t.category = 'user') DESC, n DESC""").fetchall()
    return [{"name": r["name"], "category": r["category"], "count": r["n"], "user_count": r["by_user"]} for r in rows]
