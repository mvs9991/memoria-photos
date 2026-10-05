"""Creations: a collage, a memory movie, or an animation, made from chosen photos.

Each is a new file in the upload folder under Creations/<YYYY>/<MM> (by the newest
photo's date), indexed like any other, so it can be shared, put in albums or exported.
The photos it is made from are only read. The movie has no soundtrack: none can be
bundled with an offline app, and fetching one would break "local by default".
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .. import imaging
from ..metadata import ts_to_naive
from ..rotation import rotate_image
from ..pipeline.scanner import long_path
from .uploads import _claim, ensure_upload_root


class CreateError(ValueError):
    pass


def _load(conn: sqlite3.Connection, photo_ids: list[int], side: int, images_only: bool = True) -> list[tuple[Image.Image, float | None]]:
    out = []
    for pid in photo_ids:
        r = conn.execute("SELECT p.*, r.path AS root FROM photos p JOIN roots r ON r.id = p.root_id "
                         "WHERE p.id = ? AND p.status = 'ok'", (pid,)).fetchone()
        if r is None or (images_only and r["media_type"] != "image"):
            continue
        try:
            img = imaging.decode(long_path(str(Path(r["root"]) / r["rel_path"])), max_side=side).image
        except (imaging.DecodeError, OSError):
            continue
        out.append((rotate_image(img.convert("RGB"), r["rotation"] or 0), r["taken_ts"]))
    return out


def _dest(ctx, conn: sqlite3.Connection, stamps: list[float | None], name: str) -> Path:
    _, root = ensure_upload_root(ctx, conn)
    latest = max((t for t in stamps if t), default=None)
    when = ts_to_naive(latest) if latest else None
    d = root / "Creations" / (when.strftime("%Y") if when else "Undated") / (when.strftime("%m") if when else "")
    d.mkdir(parents=True, exist_ok=True)
    return _claim(d, name)


def _touch(path: Path, stamps: list[float | None]) -> None:
    """The file's date is the newest photo's, so it sits with them in the timeline."""
    latest = max((t for t in stamps if t), default=None)
    if latest:
        try:
            ts = ts_to_naive(latest).timestamp()
            os.utime(path, (ts, ts))
        except (OSError, OverflowError, ValueError):
            pass


def _grid(n: int) -> tuple[int, int]:
    cols = math.ceil(math.sqrt(n))
    return cols, math.ceil(n / cols)


def collage(ctx, conn: sqlite3.Connection, photo_ids: list[int], size: int = 2400, gap: int = 12,
            background: tuple[int, int, int] = (250, 249, 247)) -> dict:
    if not 2 <= len(photo_ids) <= 9:
        raise CreateError("choose 2 to 9 photos for a collage")
    photos = _load(conn, photo_ids, 1600)
    if len(photos) < 2:
        raise CreateError("fewer than two of those photos could be read")
    cols, rows = _grid(len(photos))
    cell_w = (size - gap * (cols + 1)) // cols
    cell_h = int(cell_w * 3 / 4)
    canvas = Image.new("RGB", (size, rows * cell_h + gap * (rows + 1)), background)
    for i, (img, _) in enumerate(photos):
        r, c = divmod(i, cols)
        tile = ImageOps.fit(img, (cell_w, cell_h), Image.Resampling.LANCZOS, centering=(0.5, 0.4))
        canvas.paste(tile, (gap + c * (cell_w + gap), gap + r * (cell_h + gap)))
    stamps = [t for _, t in photos]
    dest = _dest(ctx, conn, stamps, f"Collage {time.strftime('%Y-%m-%d %H%M%S')}.jpg")
    canvas.save(dest, "JPEG", quality=90)
    _touch(dest, stamps)
    return {"path": str(dest), "width": canvas.width, "height": canvas.height, "photos": len(photos)}


def animation(ctx, conn: sqlite3.Connection, photo_ids: list[int], fps: int = 6, side: int = 720) -> dict:
    """A looping GIF — for a burst, it plays the moment."""
    if not 2 <= len(photo_ids) <= 60:
        raise CreateError("choose 2 to 60 photos for an animation")
    photos = _load(conn, photo_ids, side)
    if len(photos) < 2:
        raise CreateError("fewer than two of those photos could be read")
    photos.sort(key=lambda p: p[1] or 0)
    w, h = photos[0][0].size
    frames = [ImageOps.fit(img, (w, h), Image.Resampling.LANCZOS) for img, _ in photos]
    stamps = [t for _, t in photos]
    dest = _dest(ctx, conn, stamps, f"Animation {time.strftime('%Y-%m-%d %H%M%S')}.gif")
    frames[0].save(dest, "GIF", save_all=True, append_images=frames[1:], duration=int(1000 / max(1, min(fps, 30))),
                   loop=0, optimize=True)
    _touch(dest, stamps)
    return {"path": str(dest), "frames": len(frames)}


def movie(ctx, conn: sqlite3.Connection, photo_ids: list[int], seconds_each: float = 3.0,
          size: tuple[int, int] = (1280, 720), fps: int = 30, fade: float = 0.6,
          progress=None, should_stop=None) -> dict:
    """An H.264 slideshow: each photo drifts gently (a slow zoom), crossfading into the next."""
    import av

    if not 2 <= len(photo_ids) <= 150:
        raise CreateError("choose 2 to 150 photos for a movie")
    photos = _load(conn, photo_ids, max(size) * 2)
    if len(photos) < 2:
        raise CreateError("fewer than two of those photos could be read")
    photos.sort(key=lambda p: p[1] or 0)
    W, H = size
    per = max(1, int(seconds_each * fps))
    fade_n = min(per // 2, int(fade * fps))

    def frames_of(img: Image.Image) -> list[np.ndarray]:
        # Fit a slightly larger canvas, then crop a window that zooms from 100 % to 108 %.
        base = ImageOps.fit(img, (int(W * 1.1), int(H * 1.1)), Image.Resampling.LANCZOS)
        out = []
        for k in range(per):
            z = 1.0 + 0.08 * k / max(1, per - 1)
            cw, ch = int(W * 1.1 / z), int(H * 1.1 / z)
            x0, y0 = (base.width - cw) // 2, (base.height - ch) // 2
            out.append(np.asarray(base.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.Resampling.BILINEAR)))
        return out

    stamps = [t for _, t in photos]
    dest = _dest(ctx, conn, stamps, f"Memories {time.strftime('%Y-%m-%d %H%M%S')}.mp4")
    tmp = dest.with_name(dest.stem + ".part.mp4")
    out = av.open(str(tmp), "w")
    try:
        stream = out.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = W, H, "yuv420p"
        stream.options = {"preset": "veryfast", "crf": "21"}
        prev_tail: list[np.ndarray] | None = None
        for i, (img, _) in enumerate(photos):
            if should_stop and should_stop():
                break
            fr = frames_of(img)
            if prev_tail is not None:
                for k in range(fade_n):
                    a = (k + 1) / (fade_n + 1)
                    fr[k] = (prev_tail[k] * (1 - a) + fr[k] * a).astype(np.uint8)
            body = fr if i == len(photos) - 1 else fr[:-fade_n] if fade_n else fr
            for f in body:
                for pkt in stream.encode(av.VideoFrame.from_ndarray(f, format="rgb24")):
                    out.mux(pkt)
            prev_tail = fr[-fade_n:] if fade_n else None
            if progress:
                progress(i + 1, len(photos))
        for pkt in stream.encode():
            out.mux(pkt)
    finally:
        out.close()
    os.replace(tmp, dest)
    _touch(dest, stamps)
    return {"path": str(dest), "photos": len(photos), "seconds": round(len(photos) * seconds_each, 1)}
