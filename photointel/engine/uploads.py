"""Adding photos through the app — from a phone's browser, or a shared album's visitor.

An upload is written to the upload folder (Settings.upload_folder, default
`<data>/uploads`), which is registered as a library root, under
`<who>/<YYYY>/<MM>/<original name>` by the photo's own capture date. It is streamed to a
`.incoming` dot-folder (which the scanner skips), hashed on the way, and only renamed into
place when complete, so a dropped connection never leaves half a photo in the library.

A file whose exact bytes are already in the library is not stored again: a whole camera
roll can be uploaded twice and only the new photos are kept. Nothing existing is ever
overwritten — a name already taken gets " (2)".
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from ..config import SUPPORTED_EXTENSIONS
from ..metadata import date_from_filename, parse_exif_datetime
from ..pipeline.scanner import ensure_root

CHUNK = 1 << 20
MAX_BYTES = 20 << 30          # one file; a phone video is far below this


class UploadError(ValueError):
    pass


@dataclass
class Saved:
    status: str               # "added" | "duplicate" | "rejected"
    filename: str
    path: str | None = None
    reason: str | None = None
    photo_id: int | None = None


def upload_root(ctx) -> Path:
    folder = (ctx.settings.upload_folder or "").strip()
    return Path(folder).resolve() if folder else (ctx.paths.data / "uploads").resolve()


def ensure_upload_root(ctx, conn: sqlite3.Connection) -> tuple[int, Path]:
    root = upload_root(ctx)
    root.mkdir(parents=True, exist_ok=True)
    return ensure_root(conn, root), root


# Names Windows reserves for devices: "CON.jpg", "nul.tar.jpg" and a folder called "Aux" all open the
# device (or fail) instead of making a file, so an upload named that way vanished or became a 500.
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
NAME_LIMIT = 100              # one file name; a long one overflows Windows' 260-character path limit
FOLDER_LIMIT = 60             # one folder name (a user, or a shared album's name)


def _safe(name: str, limit: int = NAME_LIMIT) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in name).strip(" .")
    if len(out) > limit:                   # keep the extension: it is what makes it a photo
        stem, ext = os.path.splitext(out)
        out = (stem[:max(1, limit - len(ext))] + ext) if len(ext) < limit else out[:limit]
    out = out.strip(" .")
    if out.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES:
        out = "_" + out
    return out or "upload"


def _capture_date(path: Path, filename: str) -> datetime | None:
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = exif.get_ifd(0x8769) if exif else {}
            for raw in (sub.get(36867), sub.get(36868), exif.get(306) if exif else None):
                dt = parse_exif_datetime(raw)
                if dt:
                    return dt
    except Exception:
        pass
    dt, _, _ = date_from_filename(filename)
    return dt


def _claim(dest_dir: Path, name: str) -> Path:
    """Reserve a free file name atomically (exclusive create), so two uploads of different
    photos with the same name can never pick the same path and overwrite each other."""
    stem, ext = Path(name).stem, Path(name).suffix
    for n in range(1, 100_000):
        cand = dest_dir / (name if n == 1 else f"{stem} ({n}){ext}")
        try:
            with open(cand, "xb"):
                return cand
        except FileExistsError:
            continue
    raise UploadError(f"no free name for {name}")


def save_upload(ctx, conn: sqlite3.Connection, stream: BinaryIO, filename: str, who: str = "Phone",
                subfolder: str | None = None, album_id: int | None = None) -> Saved:
    """Store one uploaded file. `who` names the top folder (a user, or "Shared" for a
    shared album's visitors); with `album_id` the photo also goes into that album."""
    name = _safe(Path(filename or "upload").name)
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return Saved("rejected", name, reason=f"{ext or 'no extension'} is not a photo or video Memoria reads")
    _, root = ensure_upload_root(ctx, conn)
    incoming = root / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    # A name the OS guarantees is unique: two uploads at once must never share a temp file
    # (a clock-based name did collide on Windows, and one photo got the other's bytes).
    fd, tmp_name = tempfile.mkstemp(dir=incoming, suffix=ext)
    tmp = Path(tmp_name)
    h = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                block = stream.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > MAX_BYTES:
                    raise UploadError("file is too large")
                h.update(block)
                out.write(block)
        if size == 0:
            raise UploadError("empty file")
        digest = h.hexdigest()
        # Someone else's private photo is not "already in the library" for this person: their copy
        # must arrive (and nothing about the other one is revealed).
        dup = conn.execute("SELECT id, status FROM photos WHERE sha256 = ? AND status != 'deleted' "
                           "AND (private_to IS NULL OR private_to = (SELECT id FROM users WHERE username = ?)) "
                           "ORDER BY status = 'ok' DESC LIMIT 1", (digest, who)).fetchone()
        if dup is not None:
            where = "in the Trash" if dup["status"] == "trashed" else "already in your library"
            if album_id is not None and dup["status"] == "ok":        # no second copy, but it joins the album
                from .albums import add_photos
                add_photos(conn, album_id, [int(dup["id"])])
            return Saved("duplicate", name, reason=where, photo_id=int(dup["id"]))
        # uploaded a moment ago and not indexed yet
        earlier = conn.execute("SELECT path, who FROM uploads WHERE sha256 = ?", (digest,)).fetchone()
        if earlier is not None and Path(earlier[0]).exists() and (earlier["who"] == who or not conn.execute(
                "SELECT 1 FROM users WHERE username = ? AND private_uploads = 1", (earlier["who"] or "",)).fetchone()):
            if album_id is not None:
                conn.execute("UPDATE uploads SET album_id = COALESCE(album_id, ?) WHERE sha256 = ?", (album_id, digest))
                conn.commit()
            return Saved("duplicate", name, reason="already uploaded", path=earlier[0])
        when = _capture_date(tmp, name)
        parts = [_safe(who, FOLDER_LIMIT), *(([_safe(subfolder, FOLDER_LIMIT)]) if subfolder else []),
                 *((when.strftime("%Y"), when.strftime("%m")) if when else ("Undated",))]
        dest_dir = root.joinpath(*parts)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _claim(dest_dir, name)
        os.replace(tmp, dest)                  # over our own empty claim, never someone else's file
        conn.execute("INSERT OR REPLACE INTO uploads(sha256, path, who, added_at, album_id, linked) VALUES (?,?,?,?,?,0)",
                     (digest, str(dest), who, time.time(), album_id))
        conn.commit()
        if when:
            ts = when.timestamp()
            try:
                os.utime(dest, (ts, ts))       # the file's date is the photo's, not the upload's
            except (OSError, OverflowError, ValueError):
                pass
        return Saved("added", name, path=str(dest))
    except UploadError as exc:
        return Saved("rejected", name, reason=str(exc))
    finally:
        tmp.unlink(missing_ok=True)


def link_to_albums(conn: sqlite3.Connection) -> dict:
    """After indexing: put photos sent through a shared album into that album."""
    from .albums import add_photos

    rows = conn.execute(
        """SELECT u.sha256, u.album_id, p.id FROM uploads u JOIN photos p ON p.sha256 = u.sha256 AND p.status = 'ok'
           JOIN albums a ON a.id = u.album_id AND a.hidden = 0 AND a.kind = 'manual'
           WHERE u.album_id IS NOT NULL AND u.linked = 0""").fetchall()
    by_album: dict[int, list[int]] = {}
    for r in rows:
        by_album.setdefault(int(r["album_id"]), []).append(int(r["id"]))
    for aid, ids in by_album.items():
        add_photos(conn, aid, ids, commit=False)
    conn.executemany("UPDATE uploads SET linked = 1 WHERE sha256 = ?", [(r["sha256"],) for r in rows])
    conn.commit()
    return {"linked": len(rows)}
