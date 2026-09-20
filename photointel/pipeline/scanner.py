"""Recursive, incremental filesystem scan. Read-only with respect to the photo library."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from ..config import SKIP_DIR_NAMES, SUPPORTED_EXTENSIONS

log = logging.getLogger(__name__)


@dataclass
class ScanStats:
    root: str
    seen: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    restored: int = 0
    skipped_dirs: int = 0
    errors: int = 0
    seconds: float = 0.0


def iter_files(root: Path, exclude: list[Path], on_error: Callable[[str, Exception], None] | None = None
               ) -> Iterator[tuple[str, int, float, float]]:
    """Yield (relative posix path, size, mtime, ctime) for supported images under root.

    Uses an explicit stack (no recursion limits) and os.scandir (stat info is free on Windows).
    Symlinked directories are not followed to avoid cycles.
    """
    root = root.resolve()
    exclude_norm = {os.path.normcase(str(p.resolve())) for p in exclude}
    stack = [str(root)]
    root_len = len(str(root)) + 1
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as exc:
            if on_error:
                on_error(current, exc)
            continue
        dirs = []
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    low = name.lower()
                    if low in SKIP_DIR_NAMES or name.startswith("."):
                        continue
                    if os.path.normcase(entry.path) in exclude_norm:
                        continue
                    dirs.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in SUPPORTED_EXTENSIONS or name.startswith("._"):  # macOS resource forks
                    continue
                st = entry.stat(follow_symlinks=False)
                if st.st_size == 0:
                    continue
                rel = entry.path[root_len:].replace("\\", "/")
                ctime = getattr(st, "st_birthtime", None) or st.st_ctime
                yield rel, int(st.st_size), float(st.st_mtime), float(ctime)
            except OSError as exc:
                if on_error:
                    on_error(entry.path, exc)
        stack.extend(sorted(dirs, reverse=True))


def ensure_root(conn: sqlite3.Connection, path: str | Path) -> int:
    p = str(Path(path).resolve())
    row = conn.execute("SELECT id FROM roots WHERE path = ?", (p,)).fetchone()
    if row:
        return int(row[0])
    cur = conn.execute("INSERT INTO roots(path, added_at) VALUES (?, ?)", (p, time.time()))
    conn.commit()
    return int(cur.lastrowid)


def scan_root(conn: sqlite3.Connection, root_id: int, root_path: Path, exclude: list[Path],
              progress: Callable[[int], None] | None = None, should_stop: Callable[[], bool] | None = None
              ) -> ScanStats:
    t0 = time.time()
    stats = ScanStats(root=str(root_path))
    if not root_path.exists():
        log.warning("Library root %s is not accessible; skipping (photos are NOT marked missing)", root_path)
        stats.errors += 1
        return stats

    existing: dict[str, tuple[int, int, float, str]] = {
        r[0]: (r[1], r[2], r[3], r[4])
        for r in conn.execute("SELECT rel_path, id, size, mtime, status FROM photos WHERE root_id = ?", (root_id,))
    }
    now = time.time()
    seen_ids: list[int] = []
    inserts: list[tuple] = []
    changed: list[tuple] = []
    restored: list[tuple] = []

    def on_error(path: str, exc: Exception) -> None:
        stats.errors += 1
        log.warning("Scan error at %s: %s", path, exc)

    def flush() -> None:
        if inserts:
            conn.executemany(
                "INSERT INTO photos(root_id, rel_path, folder, filename, ext, size, mtime, ctime, status, "
                "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?)",
                inserts,
            )
            inserts.clear()
        if changed:
            conn.executemany(
                "UPDATE photos SET size=?, mtime=?, ctime=?, status='pending', error=NULL, meta_version=NULL, "
                "faces_model=NULL, semantic_model=NULL, last_seen_at=? WHERE id=?",
                changed,
            )
            changed.clear()
        if restored:
            conn.executemany(
                "UPDATE photos SET status = CASE WHEN meta_version IS NULL THEN 'pending' "
                "WHEN error IS NOT NULL THEN 'error' ELSE 'ok' END, last_seen_at=? WHERE id=?",
                restored,
            )
            restored.clear()
        conn.commit()

    for rel, size, mtime, ctime in iter_files(root_path, exclude, on_error):
        stats.seen += 1
        prev = existing.get(rel)
        if prev is None:
            folder, _, filename = rel.rpartition("/")
            ext = os.path.splitext(filename)[1].lower()
            inserts.append((root_id, rel, folder, filename, ext, size, mtime, ctime, now, now))
            stats.new += 1
        else:
            pid, psize, pmtime, pstatus = prev
            seen_ids.append(pid)
            if psize != size or abs(pmtime - mtime) > 1.0:
                changed.append((size, mtime, ctime, now, pid))
                stats.changed += 1
            elif pstatus == "missing":
                restored.append((now, pid))
                stats.restored += 1
            else:
                stats.unchanged += 1
        if stats.seen % 2000 == 0:
            flush()
            if progress:
                progress(stats.seen)
            if should_stop and should_stop():
                flush()
                stats.seconds = time.time() - t0
                return stats  # partial scan: do not mark anything missing
    flush()

    # Anything not seen in a *complete* scan is missing (kept in DB; analysis retained for moves).
    seen_set = set(seen_ids)
    missing = [(pid,) for rel, (pid, _, _, status) in existing.items() if pid not in seen_set and status != "missing"]
    if existing and stats.seen == 0:
        # An empty root that used to contain photos is almost always an unmounted drive/share.
        log.warning("Root %s appears empty but has %d indexed photos; not marking them missing", root_path, len(existing))
        missing = []
    if missing:
        conn.executemany("UPDATE photos SET status='missing' WHERE id=?", missing)
    stats.missing = len(missing)
    # Record the scan even on the very first pass, when nothing was "seen" before.
    conn.execute("UPDATE roots SET last_scan_at=? WHERE id=?", (now, root_id))
    conn.commit()
    stats.seconds = time.time() - t0
    log.info("Scan %s: %s", root_path, stats)
    return stats
