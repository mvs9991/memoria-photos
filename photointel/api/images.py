"""Image delivery: thumbnails, viewer previews, face crops, originals.

Originals are only ever read. Generated derivatives live in the cache directory
and are keyed by content hash so they survive moves and are shared by duplicates.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image

from .. import imaging
from ..config import RAW_EXTENSIONS
from ..rotation import rotate_image
from .deps import get_state, guard_locked

log = logging.getLogger(__name__)
router = APIRouter()

# "private": only this browser (and its offline copy) may keep them, never a shared cache.
IMMUTABLE = "private, max-age=31536000, immutable"
BROWSER_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _photo_row(photo_id: int):
    conn = get_state().conn()
    row = conn.execute(
        "SELECT p.*, r.path AS root FROM photos p JOIN roots r ON r.id = p.root_id WHERE p.id = ?", (photo_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "photo not found")
    guard_locked(row)
    return row


def abs_path(row) -> Path:
    if row["status"] == "trashed":            # still viewable from the Trash page
        from ..engine.trash import trashed_path

        moved = trashed_path(get_state().conn(), int(row["id"]))
        if moved is not None:
            return moved
    return Path(row["root"]) / row["rel_path"]


def _send_image(img: Image.Image, fmt: str = "JPEG", quality: int = 85) -> Response:
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, "JPEG", quality=quality, progressive=True)
        media = "image/jpeg"
    else:
        img.save(buf, "WEBP", quality=quality, method=0)
        media = "image/webp"
    return Response(buf.getvalue(), media_type=media, headers={"Cache-Control": IMMUTABLE})


@router.get("/thumb/{photo_id}")
def thumb(photo_id: int, s: str = Query("m", pattern="^(sm|m|l)$")):
    """s=sm (256) | m (cached 512) | l (2048 viewer preview), turned by the user's rotation."""
    row = _photo_row(photo_id)
    rot = row["rotation"] or 0
    if not rot:
        return _thumb_plain(photo_id, s, row)
    # A turned copy, cached per photo and angle (duplicates share the unturned one by hash).
    ext = "jpg" if s == "l" else "webp"
    cache = get_state().ctx.paths.thumbs / "rotated" / f"{row['sha256'] or 'x'}_{photo_id}_{s}_{rot}.{ext}"
    media = "image/jpeg" if s == "l" else "image/webp"
    if cache.exists():
        hit = _read_cached(cache)
        if hit is not None:
            return Response(hit, media_type=media, headers={"Cache-Control": IMMUTABLE})
        # Unreadable right now because another request is swapping it in: fall through
        # and rebuild rather than fail. The result is identical either way.
    plain = _thumb_plain(photo_id, s, row)
    data = Path(plain.path).read_bytes() if isinstance(plain, FileResponse) else plain.body
    img = rotate_image(Image.open(io.BytesIO(data)).convert("RGB"), rot)
    buf = io.BytesIO()
    img.save(buf, "JPEG" if s == "l" else "WEBP", quality=86 if s == "l" else 78)
    cache.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp per request: several requests for one photo can arrive together (the
    # viewer asks for the preview and the original at once).
    fd, tmp = tempfile.mkstemp(dir=cache.parent, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(buf.getvalue())
    try:
        os.replace(tmp, cache)
    except OSError:                        # another request put it there first (Windows: in use)
        Path(tmp).unlink(missing_ok=True)
    # Serve what we just built rather than re-opening the file. Returning a FileResponse
    # here was the bug: it opens the path later, while sending, and on Windows that open
    # fails outright if a concurrent request is mid-replace on the same name.
    return Response(buf.getvalue(), media_type=media, headers={"Cache-Control": IMMUTABLE})


def _read_cached(path: Path) -> bytes | None:
    """Cached bytes, or None if the file is momentarily unreadable.

    Windows refuses to open a file another thread is replacing, so a brief retry turns
    a 500 into a hit; a miss just means the caller regenerates.
    """
    for attempt in range(3):
        try:
            return path.read_bytes()
        except OSError:
            if attempt == 2:
                return None
            time.sleep(0.02)
    return None


def _cached_file(path: Path, media: str) -> Response | None:
    """Serve a cached thumbnail or preview from its bytes, not as a FileResponse.

    A FileResponse measures the file, then opens it. Another request generating the same thumbnail
    can swap the file in between, and the response then promises a length it does not deliver
    ("Response content shorter than Content-Length": a broken image on the phone). These files are
    small, so reading them first costs nothing and the length always matches."""
    data = _read_cached(path)
    if data is None:
        return None
    return Response(data, media_type=media, headers={"Cache-Control": IMMUTABLE})


def _thumb_plain(photo_id: int, s: str, row) -> Response:
    state = get_state()
    paths = state.ctx.paths
    sha = row["sha256"]

    if s == "m" and sha:
        p = imaging.thumb_path(paths.thumbs, sha)
        if p.exists() and (hit := _cached_file(p, "image/webp")) is not None:
            return hit
    if s == "sm" and sha:
        small = paths.thumbs / sha[:2] / f"{sha}_sm.webp"
        if small.exists() and (hit := _cached_file(small, "image/webp")) is not None:
            return hit
        src = imaging.thumb_path(paths.thumbs, sha)
        if src.exists():
            try:
                img = Image.open(src).convert("RGB")
                imaging.save_thumbnail(img, small, 256, quality=72)
                if (hit := _cached_file(small, "image/webp")) is not None:
                    return hit
            except Exception:
                log.debug("small thumb generation failed", exc_info=True)
    if s == "l" and sha:
        preview = paths.previews / sha[:2] / f"{sha}.jpg"
        if preview.exists() and (hit := _cached_file(preview, "image/jpeg")) is not None:
            return hit

    # Generate on demand (cache miss, cleared cache, or a photo that failed analysis).
    path = abs_path(row)
    if not path.exists():
        raise HTTPException(410, "original file is missing")
    size = {"sm": 256, "m": state.ctx.settings.thumb_size, "l": state.ctx.settings.preview_size}[s]
    try:
        dec = imaging.decode(path, max_side=size)
    except imaging.DecodeError as exc:
        raise HTTPException(415, f"cannot decode image: {exc}")
    if sha:
        try:
            if s == "l":
                imaging.save_thumbnail(dec.image, paths.previews / sha[:2] / f"{sha}.jpg", size, quality=86)
            elif s == "m":
                imaging.save_thumbnail(dec.image, imaging.thumb_path(paths.thumbs, sha), size)
            else:
                imaging.save_thumbnail(dec.image, paths.thumbs / sha[:2] / f"{sha}_sm.webp", size, quality=72)
        except Exception:
            log.debug("thumb cache write failed", exc_info=True)
    return _send_image(dec.image, "JPEG" if s == "l" else "WEBP", 86 if s == "l" else 78)


@router.get("/photos/{photo_id}/original")
def original(photo_id: int):
    """Serve the original bytes when the browser can display them, else a large preview."""
    row = _photo_row(photo_id)
    if row["rotation"]:
        return thumb(photo_id, s="l")          # the file itself is not turned; its preview is
    path = abs_path(row)
    if not path.exists():
        raise HTTPException(410, "original file is missing")
    ext = path.suffix.lower()
    if ext in BROWSER_EXT and (row["size"] or 0) < 25 << 20:
        media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                 "gif": "image/gif"}[ext.lstrip(".")]
        return FileResponse(path, media_type=media, headers={"Cache-Control": IMMUTABLE})
    return thumb(photo_id, s="l")


@router.get("/photos/{photo_id}/download")
def download(photo_id: int):
    row = _photo_row(photo_id)
    path = abs_path(row)
    if not path.exists():
        raise HTTPException(410, "original file is missing")
    return FileResponse(path, filename=row["filename"], media_type="application/octet-stream")


@router.get("/faces/{face_id}/crop")
def face_crop(face_id: int, size: int = Query(200, ge=64, le=512)):
    """Aligned-ish square crop around a face, cached on disk."""
    state = get_state()
    conn = state.conn()
    face = conn.execute(
        "SELECT f.*, p.sha256, p.locked, p.status, p.private_to FROM faces f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.id = ?",
        (face_id,)).fetchone()
    if face is None:
        raise HTTPException(404, "face not found")
    guard_locked(face)
    cache = state.ctx.paths.faces / f"{face_id % 100:02d}" / f"{face_id}_{size}.jpg"
    if cache.exists() and (hit := _cached_file(cache, "image/jpeg")) is not None:
        return hit

    row = _photo_row(face["photo_id"])
    path = abs_path(row)
    src_img = None
    sha = row["sha256"]
    if sha:  # prefer a cached derivative: far cheaper than decoding a 12 MP original
        for candidate, min_side in ((state.ctx.paths.previews / sha[:2] / f"{sha}.jpg", 1400),
                                    (imaging.thumb_path(state.ctx.paths.thumbs, sha), 400)):
            if candidate.exists():
                try:
                    img = Image.open(candidate).convert("RGB")
                    box_px = (face["x2"] - face["x1"]) * img.width
                    if box_px >= size * 0.55 or max(img.size) >= min_side:
                        src_img = img
                        break
                except Exception:
                    continue
    if src_img is None:
        if not path.exists():
            raise HTTPException(410, "original file is missing")
        try:
            src_img = imaging.decode(path, max_side=1600).image
        except imaging.DecodeError as exc:
            raise HTTPException(415, str(exc))

    W, H = src_img.size
    x1, y1, x2, y2 = face["x1"] * W, face["y1"] * H, face["x2"] * W, face["y2"] * H
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    # Square crop with a little context, shifted (not clipped) to stay inside the
    # frame — clipping produced lopsided crops for faces near an edge.
    half = min(max(x2 - x1, y2 - y1) * 0.78, W / 2, H / 2)
    cx = min(max(cx, half), W - half)
    cy = min(max(cy, half), H - half)
    box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
    crop = src_img.crop(box).resize((size, size), Image.Resampling.LANCZOS)
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Written aside and swapped in: People asks for many faces at once, and a second request for the same
    # face arriving mid-write used to serve the half-written file (a broken face).
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}-{threading.get_ident():x}.tmp")
    try:
        crop.save(tmp, "JPEG", quality=88)
        os.replace(tmp, cache)
    except Exception:
        log.debug("face crop cache write failed", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return _send_image(crop, "JPEG", 88)
