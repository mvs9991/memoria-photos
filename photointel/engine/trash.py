"""Trash: the one place Memoria removes a file, and only because the user asked.

Deleting moves a photo — with its Live-photo video and any `<name>.*.json`/`.xmp`
sidecars — into a trash folder by *renaming* it. The trash folder is on the same drive
as the photo (`<data>/trash` when the data directory shares the drive, otherwise
`<root>/.memoria-trash`, which the scanner skips as a dot-directory), so a delete is
instant, needs no free space, and a crash mid-way cannot lose a file: a rename either
happened or it did not. Nothing is ever copied and then removed.

Files stay in the trash for `Settings.trash_days` (30 by default) and can be restored
to where they were. Only then — or when the user empties the trash — are they erased.

Safety rules, each pinned by a test:
* Only the API routes in `api/routes_trash.py` (a user's click, with a confirmation
  count that must match) and the expiry sweep call into this module. Indexing,
  clustering, duplicates, clean-up lists and the optional Claude layer cannot.
* `allow_delete = False` in Settings turns deleting off entirely.
* Every move, restore and erase is written to the audit log.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from .. import db

log = logging.getLogger(__name__)

TRASH_DIR_NAME = ".memoria-trash"
SIDECAR_EXTS = (".json", ".xmp")
MAX_PER_REQUEST = 50_000


class TrashError(ValueError):
    pass


def _trash_root(ctx, root: Path, src: Path) -> Path:
    """A trash folder on the same drive as `src`, so moving is a rename."""
    data_trash = ctx.paths.data / "trash"
    try:
        if os.stat(src).st_dev == os.stat(ctx.paths.data).st_dev:
            return data_trash
    except OSError:
        pass
    return root / TRASH_DIR_NAME


def _sidecars(src: Path) -> list[Path]:
    """`IMG_1.jpg.json`, `IMG_1.jpg.supplemental-metadata.json`, `IMG_1.jpg.xmp` — named after
    this exact file. A bare `IMG_1.xmp` is left alone: it may belong to IMG_1.MOV as well."""
    prefix = src.name.lower() + "."
    try:
        return [e for e in src.parent.iterdir()
                if e.is_file() and e.name.lower().startswith(prefix) and e.suffix.lower() in SIDECAR_EXTS]
    except OSError:
        return []


def _companions(conn: sqlite3.Connection, photo_ids: list[int]) -> list[int]:
    """The video half of a Live photo goes with its still."""
    out = list(dict.fromkeys(photo_ids))
    for i in range(0, len(photo_ids), 900):
        chunk = photo_ids[i:i + 900]
        for (vid,) in conn.execute(
                f"SELECT live_video_id FROM photos WHERE id IN ({','.join('?' * len(chunk))}) "
                "AND live_video_id IS NOT NULL", chunk):
            if vid not in out:
                out.append(int(vid))
    return out


def _affected_people(conn: sqlite3.Connection, photo_ids: list[int]) -> list[int]:
    out: set[int] = set()
    for i in range(0, len(photo_ids), 900):
        chunk = photo_ids[i:i + 900]
        out |= {int(r[0]) for r in conn.execute(
            f"SELECT DISTINCT person_id FROM faces WHERE person_id IS NOT NULL AND photo_id IN "
            f"({','.join('?' * len(chunk))})", chunk)}
    return sorted(out)


def _after_change(conn: sqlite3.Connection, photo_ids: list[int]) -> None:
    """Refresh what depends on which photos are visible. The files have already moved and
    that is committed; a failure here only leaves counts stale until the next index."""
    from .people import update_person_stats

    try:
        people = _affected_people(conn, photo_ids)
        if people:
            update_person_stats(conn, people)
        db.bump_generation(conn, "embeddings")      # the search matrix holds only 'ok' photos
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        log.exception("Trash: refreshing counts failed; they will be rebuilt on the next index")


def move_to_trash(ctx, conn: sqlite3.Connection, photo_ids: list[int], actor: str = "user") -> dict:
    """Move files to the trash. Returns counts; files that cannot be moved are listed, not forced."""
    if not ctx.settings.allow_delete:
        raise TrashError("deleting files is turned off in Settings")
    if len(photo_ids) > MAX_PER_REQUEST:
        raise TrashError(f"at most {MAX_PER_REQUEST:,} files at a time")
    ids = _companions(conn, [int(p) for p in photo_ids])
    now = time.time()
    expires = now + max(1, int(ctx.settings.trash_days)) * 86400
    moved_ids: list[int] = []
    skipped: list[dict] = []
    total = 0
    for pid in ids:
        row = conn.execute("SELECT p.id, p.rel_path, p.status, p.size, r.path AS root FROM photos p "
                           "JOIN roots r ON r.id = p.root_id WHERE p.id = ?", (pid,)).fetchone()
        if row is None:
            skipped.append({"photo_id": pid, "reason": "not in the library"})
            continue
        if row["status"] in ("trashed", "deleted"):
            continue
        root = Path(row["root"])
        src = root / row["rel_path"]
        if not src.is_file():
            skipped.append({"photo_id": pid, "reason": "file not found"})
            continue
        files = [src, *_sidecars(src)]
        cur = conn.execute(
            "INSERT INTO trash(photo_id, files, size, prev_status, state, trashed_at, expires_at) "
            "VALUES (?, '[]', ?, ?, 'pending', ?, ?)", (pid, int(row["size"] or 0), row["status"], now, expires))
        tid = int(cur.lastrowid)
        dest_dir = _trash_root(ctx, root, src) / str(tid)
        plan = [(str(f), str(dest_dir / f.name)) for f in files]
        conn.execute("UPDATE trash SET files = ? WHERE id = ?", (json.dumps(plan), tid))
        conn.commit()                      # the plan is on disk before any file moves
        done: list[tuple[str, str]] = []
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for a, b in plan:
                os.rename(a, b)            # same drive by construction; never copy + delete
                done.append((a, b))
        except OSError as exc:
            undone = True
            for a, b in reversed(done):
                try:
                    os.rename(b, a)
                except OSError:
                    undone = False
                    log.error("Could not put %s back (%s); it is safe in the trash at %s", a, exc, b)
            if undone:
                conn.execute("DELETE FROM trash WHERE id = ?", (tid,))
                _rmdir_quiet(dest_dir)
                conn.commit()
                skipped.append({"photo_id": pid, "reason": f"could not move: {exc.strerror or exc}"})
                continue
            # Half moved and stuck: keep it listed in the trash, where Restore can bring it back.
        conn.execute("UPDATE trash SET state = 'trashed' WHERE id = ?", (tid,))
        conn.execute("UPDATE photos SET status = 'trashed' WHERE id = ?", (pid,))
        conn.commit()
        moved_ids.append(pid)
        total += int(row["size"] or 0)
    if moved_ids:
        db.audit(conn, "photos_trashed", "photo", None, {"photos": moved_ids[:2000], "count": len(moved_ids),
                                                          "bytes": total, "days": ctx.settings.trash_days}, actor=actor)
        _after_change(conn, moved_ids)
    return {"trashed": len(moved_ids), "bytes": total, "skipped": skipped, "expires_at": expires}


def restore(ctx, conn: sqlite3.Connection, photo_ids: list[int]) -> dict:
    """Put files back where they were (a taken name gets ' (restored)')."""
    ids = _companions(conn, [int(p) for p in photo_ids])
    restored, failed = [], []
    for pid in ids:
        entry = conn.execute("SELECT * FROM trash WHERE photo_id = ? AND state = 'trashed' ORDER BY id DESC LIMIT 1",
                             (pid,)).fetchone()
        if entry is None:
            continue
        plan = json.loads(entry["files"])
        main_orig, main_trash = plan[0]
        target = _free_name(Path(main_orig))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(main_trash, target)
        except OSError as exc:
            failed.append({"photo_id": pid, "reason": str(exc.strerror or exc)})
            continue
        for orig, tr in plan[1:]:
            if not Path(orig).exists() and Path(tr).exists():
                try:
                    os.rename(tr, orig)
                except OSError:
                    log.warning("Sidecar %s left in the trash", tr)
        if target != Path(main_orig):          # the old name was taken: follow the file
            row = conn.execute("SELECT r.path FROM photos p JOIN roots r ON r.id = p.root_id WHERE p.id = ?",
                               (pid,)).fetchone()
            if row is not None:
                rel = target.relative_to(Path(row[0])).as_posix()
                conn.execute("UPDATE photos SET rel_path = ?, filename = ? WHERE id = ?", (rel, target.name, pid))
        conn.execute("UPDATE photos SET status = ? WHERE id = ?", (entry["prev_status"], pid))
        conn.execute("UPDATE trash SET state = 'restored', finished_at = ? WHERE id = ?", (time.time(), entry["id"]))
        _rmdir_quiet(Path(main_trash).parent)
        conn.commit()
        restored.append(pid)
    if restored:
        db.audit(conn, "photos_restored", "photo", None, {"photos": restored[:2000], "count": len(restored)})
        _after_change(conn, restored)
    return {"restored": len(restored), "failed": failed}


def purge(ctx, conn: sqlite3.Connection, photo_ids: list[int] | None = None, expired_only: bool = False,
          actor: str = "user") -> dict:
    """Erase trashed files for good: the given photos, everything, or (expired_only) what has run out."""
    sql = "SELECT * FROM trash WHERE state = 'trashed'"
    args: list = []
    if expired_only:
        sql += " AND expires_at <= ?"
        args.append(time.time())
    entries = conn.execute(sql, args).fetchall()
    if photo_ids is not None:
        want = set(_companions(conn, [int(p) for p in photo_ids]))
        entries = [e for e in entries if e["photo_id"] in want]
    erased, freed = [], 0
    for e in entries:
        plan = json.loads(e["files"])
        ok = True
        for _, tr in plan:
            try:
                Path(tr).unlink(missing_ok=True)
            except OSError as exc:
                ok = False
                log.warning("Could not erase %s: %s", tr, exc)
        if not ok:
            continue
        _rmdir_quiet(Path(plan[0][1]).parent)
        conn.execute("UPDATE trash SET state = 'purged', finished_at = ? WHERE id = ?", (time.time(), e["id"]))
        conn.execute("UPDATE photos SET status = 'deleted' WHERE id = ? AND status = 'trashed'", (e["photo_id"],))
        erased.append(int(e["photo_id"]))
        freed += int(e["size"] or 0)
    if erased:
        db.audit(conn, "photos_erased", "photo", None,
                 {"photos": erased[:2000], "count": len(erased), "bytes": freed, "expired": expired_only}, actor=actor)
    conn.commit()
    return {"erased": len(erased), "bytes": freed}


def purge_expired(ctx, conn: sqlite3.Connection) -> dict:
    return purge(ctx, conn, expired_only=True, actor="system")


def reconcile(conn: sqlite3.Connection) -> int:
    """Undo a move interrupted by a crash: anything half-moved goes back where it was."""
    fixed = 0
    for e in conn.execute("SELECT * FROM trash WHERE state = 'pending'").fetchall():
        for orig, tr in json.loads(e["files"]):
            if Path(tr).exists() and not Path(orig).exists():
                try:
                    os.rename(tr, orig)
                except OSError:
                    log.error("Interrupted delete: %s is still in the trash at %s", orig, tr)
                    continue
        if json.loads(e["files"]):
            _rmdir_quiet(Path(json.loads(e["files"])[0][1]).parent)
        conn.execute("DELETE FROM trash WHERE id = ?", (e["id"],))
        fixed += 1
    conn.commit()
    return fixed


def trashed_path(conn: sqlite3.Connection, photo_id: int) -> Path | None:
    """Where a trashed photo's file is now (for thumbnails and the viewer)."""
    e = conn.execute("SELECT files FROM trash WHERE photo_id = ? AND state = 'trashed' ORDER BY id DESC LIMIT 1",
                     (photo_id,)).fetchone()
    return Path(json.loads(e[0])[0][1]) if e else None


def list_trash(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT t.photo_id, t.size, t.trashed_at, t.expires_at, t.files, p.filename, p.width, p.height,
                  p.taken_ts, p.media_type, p.duration, p.live_component
           FROM trash t JOIN photos p ON p.id = t.photo_id
           WHERE t.state = 'trashed' ORDER BY t.trashed_at DESC, t.id DESC""").fetchall()
    out = []
    for r in rows:
        if r["live_component"]:
            continue                          # shown through its still
        out.append({"photo_id": r["photo_id"], "filename": r["filename"], "size": r["size"],
                    "trashed_at": r["trashed_at"], "expires_at": r["expires_at"],
                    "original_path": json.loads(r["files"])[0][0],
                    "ratio": round((r["width"] or 4) / max(r["height"] or 3, 1), 3), "ts": int(r["taken_ts"] or 0),
                    "video": r["media_type"] == "video", "duration": r["duration"]})
    return out


def _free_name(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(1, 1000):
        cand = path.with_name(f"{path.stem} (restored{'' if n == 1 else f' {n}'}){path.suffix}")
        if not cand.exists():
            return cand
    raise TrashError(f"no free name next to {path}")


def _rmdir_quiet(d: Path) -> None:
    try:
        d.rmdir()
    except OSError:
        pass


