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


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in name).strip().rstrip(".")
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
                subfolder: str | None = None) -> Saved:
    """Store one uploaded file. `who` names the top folder (a user, or a shared album)."""
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
        dup = conn.execute("SELECT id, status FROM photos WHERE sha256 = ? AND status != 'deleted' "
                           "ORDER BY status = 'ok' DESC LIMIT 1", (digest,)).fetchone()
        if dup is not None:
            where = "in the Trash" if dup["status"] == "trashed" else "already in your library"
            return Saved("duplicate", name, reason=where, photo_id=int(dup["id"]))
        # uploaded a moment ago and not indexed yet
        earlier = conn.execute("SELECT path FROM uploads WHERE sha256 = ?", (digest,)).fetchone()
        if earlier is not None and Path(earlier[0]).exists():
            return Saved("duplicate", name, reason="already uploaded", path=earlier[0])
        when = _capture_date(tmp, name)
        parts = [_safe(who), *(([_safe(subfolder)]) if subfolder else []),
                 *((when.strftime("%Y"), when.strftime("%m")) if when else ("Undated",))]
        dest_dir = root.joinpath(*parts)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _claim(dest_dir, name)
        os.replace(tmp, dest)                  # over our own empty claim, never someone else's file
        conn.execute("INSERT OR REPLACE INTO uploads(sha256, path, who, added_at) VALUES (?,?,?,?)",
                     (digest, str(dest), who, time.time()))
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
