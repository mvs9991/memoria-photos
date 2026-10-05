"""The phone grid's small thumbnails, packed into one file in timeline order.

Each small thumbnail is a file named by its content hash, so photos next to each other in the grid are scattered
over 256 folders on the disk. On the 5,400 rpm drive the real library lives on, a screenful of 30 tiles nobody had
looked at recently cost 30 random seeks: 1.1 s measured on the live server (avg 330 ms a tile, sequentially). In the
pack they sit in the order the grid shows them (newest first), so a screenful is one stretch of one file. (Not
measured cold: Windows gives no way to drop a file from its cache without administrator rights; that is the
expected effect of one read instead of 30, and why the pack exists.)

The pack is a cache like the thumbnails it copies: built from them when the server is idle, never from the
originals; anything not in it (a photo added since, a rotated one) is served from its file as before. Building is
resumable: the thumbnails read so far are kept in a staging file, so being interrupted by someone opening the app
only pauses it. When the staging is complete it is written out in grid order as `small-pack-<n>.db`, the server
switches to it, and the previous pack is removed. Clearing the thumbnail cache closes and removes it too.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PREFIX = "small-pack-"
STAGING = "small-pack-staging.db"
REBUILD_SHARE = 0.02          # rebuild when more than this share of visible photos is missing from the pack
REBUILD_AFTER_S = 86400       # ...or a day after the last build, if anything is missing at all

_lock = threading.Lock()
_open: dict[str, tuple[Path, sqlite3.Connection]] = {}


def _packs(thumb_root: Path) -> list[Path]:
    """Finished packs, newest (highest number) first."""
    out = []
    for p in thumb_root.glob(f"{PREFIX}*.db"):
        n = p.stem[len(PREFIX):]
        if n.isdigit():
            out.append((int(n), p))
    return [p for _, p in sorted(out, reverse=True)]


def _conn(thumb_root: Path) -> sqlite3.Connection | None:
    key = str(thumb_root)
    with _lock:
        hit = _open.get(key)
        if hit is not None:
            if hit[0].exists():
                return hit[1]
            try:                                # its file went (the cache was cleared): forget it
                hit[1].close()
            except sqlite3.Error:
                pass
            del _open[key]
        packs = _packs(thumb_root)
        if not packs:
            return None
        conn = sqlite3.connect(f"file:{packs[0].as_posix()}?mode=ro", uri=True, check_same_thread=False)
        _open[key] = (packs[0], conn)
        return conn


def lookup(thumb_root: Path, sha: str) -> bytes | None:
    """The small thumbnail's bytes from the pack, or None (no pack yet, or this photo is not in it)."""
    conn = _conn(thumb_root)
    if conn is None:
        return None
    try:
        with _lock:
            row = conn.execute("SELECT data FROM pack WHERE sha = ?", (sha,)).fetchone()
    except sqlite3.Error:
        return None
    return bytes(row[0]) if row else None


def close(thumb_root: Path | None = None) -> None:
    """Close the open pack (before its folder is emptied, or for tests)."""
    with _lock:
        for key in list(_open):
            if thumb_root is None or key == str(thumb_root):
                try:
                    _open[key][1].close()
                except sqlite3.Error:
                    pass
                del _open[key]


def status(conn: sqlite3.Connection, thumb_root: Path) -> dict:
    """How current the pack is: photos visible now, how many of them it holds, when it was built."""
    from . import thumbs as thumbs_mod

    shas = _visible_shas(conn)
    packed: set[str] = set()
    built = None
    pc = _conn(thumb_root)
    if pc is not None:
        with _lock:
            packed = {r[0] for r in pc.execute("SELECT sha FROM pack")}
            row = pc.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        built = float(row[0]) if row else None
    missing = [s for s in shas if s not in packed and thumbs_mod.small_path(thumb_root, s).exists()]
    return {"visible": len(shas), "packed": len(packed), "missing": len(missing), "built_at": built}


def needs_build(conn: sqlite3.Connection, thumb_root: Path, now: float | None = None) -> bool:
    st = status(conn, thumb_root)
    if st["missing"] == 0:
        return False
    if st["built_at"] is None:
        return True
    if st["missing"] > REBUILD_SHARE * max(1, st["visible"]):
        return True
    return (now or time.time()) - st["built_at"] >= REBUILD_AFTER_S


def _visible_shas(conn: sqlite3.Connection) -> list[str]:
    """Content hashes in the grid's order: newest first (duplicates once, at their newest place)."""
    seen: set[str] = set()
    out = []
    for (sha,) in conn.execute(
            "SELECT sha256 FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 "
            "AND sha256 IS NOT NULL ORDER BY taken_ts DESC, id DESC"):
        if sha not in seen:
            seen.add(sha)
            out.append(sha)
    return out


def build(conn: sqlite3.Connection, thumb_root: Path, *, may_run: Callable[[], bool], pause: float = 0.0) -> str:
    """Stage what is not staged yet, then (when complete) write the pack. Returns "built", "paused" or "nothing"."""
    from . import thumbs as thumbs_mod

    shas = _visible_shas(conn)
    if not shas:
        return "nothing"
    thumb_root.mkdir(parents=True, exist_ok=True)
    stage_path = thumb_root / STAGING
    stage = sqlite3.connect(stage_path)
    try:
        stage.execute("CREATE TABLE IF NOT EXISTS staged (sha TEXT PRIMARY KEY, data BLOB NOT NULL)")
        have = {r[0] for r in stage.execute("SELECT sha FROM staged")}
        # Already in the current pack: copied from there (one file, fast) rather than from 256 folders.
        current = _conn(thumb_root)
        todo = [s for s in shas if s not in have]
        # Files in name order: the folders are by the hash's first two letters, so this reads one folder at a time.
        for n, sha in enumerate(sorted(todo)):
            if not may_run():
                stage.commit()
                return "paused"
            data = None
            if current is not None:
                with _lock:
                    row = current.execute("SELECT data FROM pack WHERE sha = ?", (sha,)).fetchone()
                data = bytes(row[0]) if row else None
            if data is None:
                p = thumbs_mod.small_path(thumb_root, sha)
                try:
                    data = p.read_bytes()
                except OSError:
                    continue                    # no small thumbnail yet: the backfill makes it; next build has it
            stage.execute("INSERT OR REPLACE INTO staged(sha, data) VALUES (?, ?)", (sha, data))
            if n % 200 == 0:
                stage.commit()
            if pause:
                time.sleep(pause)
        stage.commit()
        if not may_run():
            return "paused"
        # Written in grid order, in one go: a fresh file whose rows lie on the disk in the order they are shown.
        packs = _packs(thumb_root)
        number = (int(packs[0].stem[len(PREFIX):]) + 1) if packs else 1
        final = thumb_root / f"{PREFIX}{number}.db"
        tmp = thumb_root / f"{PREFIX}{number}.tmp"
        tmp.unlink(missing_ok=True)
        out = sqlite3.connect(tmp)
        try:
            out.execute("PRAGMA journal_mode = OFF")
            out.execute("PRAGMA page_size = 16384")
            out.execute("CREATE TABLE pack (pos INTEGER PRIMARY KEY, sha TEXT NOT NULL UNIQUE, data BLOB NOT NULL)")
            out.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            staged = {r[0] for r in stage.execute("SELECT sha FROM staged")}
            pos = 0
            for sha in shas:
                if sha not in staged:
                    continue
                data = stage.execute("SELECT data FROM staged WHERE sha = ?", (sha,)).fetchone()[0]
                out.execute("INSERT INTO pack(pos, sha, data) VALUES (?, ?, ?)", (pos, sha, data))
                pos += 1
            out.execute("INSERT INTO meta VALUES ('built_at', ?)", (str(time.time()),))
            out.commit()
        finally:
            out.close()
        os.replace(tmp, final)
    finally:
        stage.close()
    # Switch to the new pack, then remove the older ones and the staging (all files this module made).
    close(thumb_root)
    for old in _packs(thumb_root)[1:]:
        try:
            old.unlink()
        except OSError:
            pass                                # still open somewhere: the next build tidies it
    try:
        stage_path.unlink()
    except OSError:
        pass
    log.info("Small-thumbnail pack %s: %d photos", final.name, pos)
    return "built"
