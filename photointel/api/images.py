"""Image delivery: thumbnails, viewer previews, face crops, originals.

Originals are only ever read. Generated derivatives live in the cache directory
and are keyed by content hash so they survive moves and are shared by duplicates.
"""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image

from .. import imaging
from ..config import RAW_EXTENSIONS
from .deps import get_state

log = logging.getLogger(__name__)
router = APIRouter()

IMMUTABLE = "public, max-age=31536000, immutable"
BROWSER_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _photo_row(photo_id: int):
    conn = get_state().conn()
    row = conn.execute(
        "SELECT p.*, r.path AS root FROM photos p JOIN roots r ON r.id = p.root_id WHERE p.id = ?", (photo_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "photo not found")
    return row


def abs_path(row) -> Path:
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
    """s=sm (256) | m (cached 512) | l (2048 viewer preview)."""
    state = get_state()
    row = _photo_row(photo_id)
    paths = state.ctx.paths
    sha = row["sha256"]

    if s == "m" and sha:
        p = imaging.thumb_path(paths.thumbs, sha)
        if p.exists():
            return FileResponse(p, media_type="image/webp", headers={"Cache-Control": IMMUTABLE})
    if s == "sm" and sha:
        small = paths.thumbs / sha[:2] / f"{sha}_sm.webp"
        if small.exists():
            return FileResponse(small, media_type="image/webp", headers={"Cache-Control": IMMUTABLE})
        src = imaging.thumb_path(paths.thumbs, sha)
        if src.exists():
            try:
                img = Image.open(src).convert("RGB")
                imaging.save_thumbnail(img, small, 256, quality=72)
                return FileResponse(small, media_type="image/webp", headers={"Cache-Control": IMMUTABLE})
            except Exception:
                log.debug("small thumb generation failed", exc_info=True)
    if s == "l" and sha:
        preview = paths.previews / sha[:2] / f"{sha}.jpg"
        if preview.exists():
            return FileResponse(preview, media_type="image/jpeg", headers={"Cache-Control": IMMUTABLE})

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
        "SELECT f.*, p.sha256 FROM faces f JOIN photos p ON p.id = f.photo_id WHERE f.id = ?", (face_id,)
    ).fetchone()
    if face is None:
        raise HTTPException(404, "face not found")
    cache = state.ctx.paths.faces / f"{face_id % 100:02d}" / f"{face_id}_{size}.jpg"
    if cache.exists():
        return FileResponse(cache, media_type="image/jpeg", headers={"Cache-Control": IMMUTABLE})

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
    try:
        crop.save(cache, "JPEG", quality=88)
    except Exception:
        log.debug("face crop cache write failed", exc_info=True)
    return _send_image(crop, "JPEG", 88)
