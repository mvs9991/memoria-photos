"""Backup: copy every photo folder and Memoria's own data to another drive.

Once the cloud is gone, the computer holds the only copy; this is the second one. It is a
copy-only mirror: new or changed files are copied, nothing already in the backup is ever
deleted or overwritten with an older version, so a photo deleted from the library (after
its 30 days in the Trash) is still in the backup. Each copy is hashed while it is written
and checked against the hash taken at indexing, which catches a failing disk or cable.

Layout:  <target>/Memoria Backup/photos/<folder name>/...   (every file in each root)
         <target>/Memoria Backup/memoria/library.db            (a consistent snapshot)
         <target>/Memoria Backup/memoria/settings.json, gpx/, backup-log.json
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .. import db
from ..pipeline.scanner import is_link_dir_path, long_path
from .trash import TRASH_DIR_NAME
from .xmp import ExportError, _check_destination

log = logging.getLogger(__name__)
CHUNK = 4 << 20
SKIP_DIRS = {TRASH_DIR_NAME, ".incoming"}
FATAL_DEST_ERRNOS = {errno.ENOSPC, errno.EROFS, getattr(errno, "EDQUOT", errno.ENOSPC)}
MAX_FAILURES_IN_A_ROW = 25


def _root_names(conn: sqlite3.Connection) -> dict[int, tuple[Path, str]]:
    roots = [(int(r[0]), Path(r[1])) for r in conn.execute("SELECT id, path FROM roots ORDER BY id")]
    names: dict[int, tuple[Path, str]] = {}
    used: set[str] = set()
    for rid, path in roots:
        base = "".join("_" if c in '<>:"/\\|?*' else c for c in (path.name or f"root{rid}")) or f"root{rid}"
        name = base if base.lower() not in used else f"{base} ({rid})"
        used.add(name.lower())
        names[rid] = (path, name)
    return names


def _copy_hashed(src: Path, dest: Path) -> str:
    """Copy through a .part file, hashing what was read, then rename into place. `src`/`dest` stay the
    plain (non-prefixed) path everywhere else in this module (a test, an error message or
    `known.get((rid, rel))` must see the same form whether or not a given path happens to be long);
    every actual filesystem call here goes through `long_path()` instead — a path over 260 characters,
    without Windows' long-paths setting on, otherwise cannot even be opened, let alone stat'd or renamed."""
    tmp = dest.with_name(dest.name + ".part")
    lsrc, ltmp, ldest = long_path(str(src)), long_path(str(tmp)), long_path(str(dest))
    h = hashlib.sha256()
    with open(lsrc, "rb") as fi, open(ltmp, "wb") as fo:
        before = os.fstat(fi.fileno())      # what the source looked like when we started reading
        for block in iter(lambda: fi.read(CHUNK), b""):
            h.update(block)
            fo.write(block)
    shutil.copystat(lsrc, ltmp)
    # Stamp the copy with the mtime the source had BEFORE it was read. If the source changed while
    # we copied, the copy may mix old and new bytes; with the new mtime it would look up to date for
    # ever. With the old one the next run sees a difference and copies it again.
    os.utime(ltmp, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(ltmp, ldest)
    return h.hexdigest()


def validate_target(ctx, conn: sqlite3.Connection, target: str | Path) -> Path:
    """A backup must not live inside a photo folder (it would back itself up) or the data folder."""
    out = _check_destination(conn, Path(target))
    data = ctx.paths.data.resolve()
    if out == data or data in out.parents:
        raise ExportError("choose a backup folder outside Memoria's data folder")
    return out


def run_backup(ctx, conn: sqlite3.Connection, target: str | Path, progress: Callable[[int, int], None] | None = None,
               should_stop: Callable[[], bool] | None = None) -> dict:
    out = validate_target(ctx, conn, target)
    base = out / "Memoria Backup"
    t0 = time.time()
    known = {(int(r[0]), r[1]): r[2] for r in conn.execute(
        "SELECT root_id, rel_path, sha256 FROM photos WHERE sha256 IS NOT NULL AND status IN ('ok', 'error', 'pending')")}

    files: list[tuple[int, Path, str, Path]] = []
    scan_errors: list[str] = []
    for rid, (root, name) in _root_names(conn).items():
        if not root.exists():
            log.warning("Backup: %s is not reachable; skipped", root)
            continue
        # Scanned via the long-path form (a path over 260 characters, without Windows' long-paths setting
        # on, made os.walk silently drop that whole subtree — its default onerror is None, so the scan
        # error vanished with no file counted as failed and nothing in the report). `src`/`rel` are rebuilt
        # on the plain root straight after, so every path this function stores, compares or reports stays
        # the ordinary form the rest of it (and the tests, and anyone reading `errors`) already expects;
        # only the few calls that touch the filesystem go through `_lp()` for the long form.
        long_root = str(long_path(str(root)))
        root_len = len(long_root.rstrip("\\/")) + 1
        for dirpath, dirnames, filenames in os.walk(long_root, onerror=lambda e: scan_errors.append(str(e))):
            # os.walk's own followlinks=False does not see a Windows junction (only a real symlink), so a
            # junction pointing outside the root — or back at one of its own ancestors, which would recurse
            # forever — was both followed and backed up as if it were part of the photo library.
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                          and not is_link_dir_path(os.path.join(dirpath, d))]
            for fn in filenames:
                rel = (dirpath[root_len:] + "/" + fn).replace(os.sep, "/").lstrip("/")
                src = root / rel
                dest = base / "photos" / name / rel
                files.append((rid, src, rel, dest))

    copied = unchanged = failed = 0
    mismatched: list[str] = []
    errors: list[dict] = []
    written = 0
    in_a_row = 0
    for n, (rid, src, rel, dest) in enumerate(files):
        if should_stop and should_stop():
            break
        try:
            st = os.stat(long_path(str(src)))
            if os.path.exists(long_path(str(dest))):
                dst = os.stat(long_path(str(dest)))
                if dst.st_size == st.st_size and abs(dst.st_mtime - st.st_mtime) < 2:
                    unchanged += 1
                    continue
                if dst.st_mtime > st.st_mtime + 2:
                    unchanged += 1      # the backup holds a newer version; never replace it with an older one
                    continue
            os.makedirs(long_path(str(dest.parent)), exist_ok=True)
            digest = _copy_hashed(src, dest)
            expected = known.get((rid, rel))
            if expected and expected != digest:
                mismatched.append(str(src))
            copied += 1
            in_a_row = 0
            written += st.st_size
        except OSError as exc:
            failed += 1
            in_a_row += 1
            errors.append({"file": str(src), "error": str(exc.strerror or exc)})
            # A full or vanished backup drive fails every file; do not grind through the library, and
            # do not record the run as a finished backup.
            if exc.errno in FATAL_DEST_ERRNOS:
                raise ExportError(f"the backup drive cannot take more: {exc.strerror or exc} "
                                  f"({copied:,} copied before it stopped)") from exc
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                raise ExportError(f"{in_a_row} files in a row could not be backed up (last: {src.name}: "
                                  f"{exc.strerror or exc}); is the drive still connected?") from exc
        if progress and (n % 50 == 0 or n == len(files) - 1):
            progress(n + 1, len(files))

    mem = base / "memoria"
    snapshot_memoria(ctx, conn, mem)

    summary = {"folder": str(base), "files": len(files), "copied": copied, "unchanged": unchanged,
               "failed": failed, "bytes": written, "hash_mismatches": mismatched[:50],
               "errors": errors[:50], "scan_errors": scan_errors[:50], "seconds": round(time.time() - t0, 1),
               "finished_at": time.time(), "cancelled": bool(should_stop and should_stop())}
    (mem / "backup-log.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    if not summary["cancelled"]:
        db.set_meta(conn, "last_backup", json.dumps(summary))
        conn.commit()
    return summary


def snapshot_memoria(ctx, conn: sqlite3.Connection, mem: Path) -> None:
    """Memoria's own data into `mem`: a consistent database snapshot (the live file may be
    mid-write), the settings and the GPS tracks. Shared with the off-site copy."""
    data = ctx.paths.data.resolve()
    mem.mkdir(parents=True, exist_ok=True)
    snap = mem / "library.db.part"
    dst_conn = sqlite3.connect(snap)
    try:
        conn.commit()
        conn.backup(dst_conn)
    finally:
        dst_conn.close()
    os.replace(snap, mem / "library.db")
    for name in ("settings.json",):
        if (data / name).exists():
            shutil.copy2(data / name, mem / name)
    if (data / "gpx").exists():
        shutil.copytree(data / "gpx", mem / "gpx", dirs_exist_ok=True)


def last_backup(conn: sqlite3.Connection) -> dict | None:
    raw = db.get_meta(conn, "last_backup")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def run_job(ctx, job_id: int | None, target: str) -> dict:
    from ..pipeline.jobs import JobReporter

    reporter = JobReporter(ctx, job_id)
    reporter.start()
    conn = ctx.connect()

    def should_stop() -> bool:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id else None
        return bool(row and row[0])

    try:
        out = run_backup(ctx, conn, target, should_stop=should_stop,
                         progress=lambda d, t: reporter.progress("backup", d, t, f"Checked {d:,} of {t:,}"))
        msg = f"Backed up {out['copied']:,} new or changed files ({out['unchanged']:,} already there)"
        if out["failed"]:
            msg += f", {out['failed']:,} failed"
        if out["hash_mismatches"]:
            msg += f", {len(out['hash_mismatches'])} did not match their hash — check the disk"
        reporter.finish("cancelled" if out["cancelled"] else "done", msg)
        return out
    except Exception as exc:
        log.exception("Backup failed")
        reporter.finish("failed", "Backup failed", str(exc))
        raise
    finally:
        conn.close()
