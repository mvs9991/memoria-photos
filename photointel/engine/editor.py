"""Editing, always as a new copy: the original is only read.

A photo edit (crop, rotate, flip, light and colour, a filter) is applied to the full-size
decoded image and saved as a JPEG named `<name> (edited).jpg` in the upload folder under
`Edits/<YYYY>/<MM>/`, carrying the original's EXIF (date, camera, GPS) with orientation
reset, because the pixels are already upright. A video trim copies the stream packets
between two times into `<name> (trim).<ext>` without re-encoding, so it is fast and
lossless; it starts at the key frame at or before the chosen start.

The new file is indexed like any other photo and shows up next to its original as an
"edited" duplicate, where either can be kept.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

from .. import imaging
from ..metadata import ts_to_naive
from ..rotation import rotate_image
from ..pipeline.scanner import long_path
from .uploads import _claim, ensure_upload_root

FILTERS = ("none", "mono", "sepia", "vivid", "fade", "warm", "cool")
FULL_SIDE = 12000            # effectively "full size" for decode()


class EditError(ValueError):
    pass


@dataclass
class EditSpec:
    rotate: int = 0                      # clockwise quarter turns, applied first
    flip: bool = False                   # mirror left-right
    crop: tuple[float, float, float, float] | None = None   # normalised x1, y1, x2, y2 after rotate/flip
    brightness: int = 0                  # -100..100
    contrast: int = 0
    saturation: int = 0
    warmth: int = 0                      # -100 (cool) .. 100 (warm)
    filter: str = "none"
    auto: bool = False                   # stretch levels (autocontrast) before the sliders

    @classmethod
    def from_dict(cls, d: dict) -> "EditSpec":
        s = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if s.rotate % 90:
            raise EditError("rotate by quarter turns")
        for k in ("brightness", "contrast", "saturation", "warmth"):
            if not -100 <= int(getattr(s, k)) <= 100:
                raise EditError(f"{k} is -100 to 100")
        if s.filter not in FILTERS:
            raise EditError(f"filter must be one of {', '.join(FILTERS)}")
        if s.crop is not None:
            x1, y1, x2, y2 = (float(v) for v in s.crop)
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1) or (x2 - x1) < 0.02 or (y2 - y1) < 0.02:
                raise EditError("crop must be a box inside the photo")
            s.crop = (x1, y1, x2, y2)
        return s

    def is_noop(self) -> bool:
        return (self.rotate % 360 == 0 and not self.flip and self.crop is None and not self.auto and self.filter == "none"
                and not any((self.brightness, self.contrast, self.saturation, self.warmth)))


def apply(img: Image.Image, spec: EditSpec) -> Image.Image:
    img = rotate_image(img.convert("RGB"), spec.rotate % 360)
    if spec.flip:
        img = ImageOps.mirror(img)
    if spec.crop:
        w, h = img.size
        x1, y1, x2, y2 = spec.crop
        img = img.crop((round(x1 * w), round(y1 * h), round(x2 * w), round(y2 * h)))
    if spec.auto:
        img = ImageOps.autocontrast(img, cutoff=0.5)
    if spec.brightness:
        img = ImageEnhance.Brightness(img).enhance(1 + spec.brightness / 100)
    if spec.contrast:
        img = ImageEnhance.Contrast(img).enhance(1 + spec.contrast / 100)
    if spec.saturation:
        img = ImageEnhance.Color(img).enhance(1 + spec.saturation / 100)
    warmth = spec.warmth + {"warm": 35, "cool": -35}.get(spec.filter, 0)
    if warmth:
        r, g, b = img.split()
        k = warmth / 100 * 0.25
        r = r.point(lambda v: min(255, max(0, round(v * (1 + k)))))
        b = b.point(lambda v: min(255, max(0, round(v * (1 - k)))))
        img = Image.merge("RGB", (r, g, b))
    if spec.filter == "mono":
        img = ImageOps.grayscale(img).convert("RGB")
    elif spec.filter == "sepia":
        img = ImageOps.colorize(ImageOps.grayscale(img), black=(40, 26, 13), white=(255, 240, 205))
    elif spec.filter == "vivid":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.35)).enhance(1.1)
    elif spec.filter == "fade":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(0.8)).enhance(0.85)
        img = Image.blend(img, Image.new("RGB", img.size, (238, 232, 222)), 0.08)
    return img


def _source(conn: sqlite3.Connection, photo_id: int):
    row = conn.execute("SELECT p.*, r.path AS root FROM photos p JOIN roots r ON r.id = p.root_id WHERE p.id = ?",
                       (photo_id,)).fetchone()
    if row is None or row["status"] != "ok":
        raise EditError("photo not found")
    path = Path(long_path(str(Path(row["root"]) / row["rel_path"])))
    if not path.exists():
        raise EditError("the original file is missing")
    return row, path


def _dest_dir(ctx, conn: sqlite3.Connection, row) -> Path:
    _, root = ensure_upload_root(ctx, conn)
    when = ts_to_naive(row["taken_ts"]) if row["taken_ts"] else None
    d = root / "Edits" / (when.strftime("%Y") if when else "Undated") / (when.strftime("%m") if when else "")
    d.mkdir(parents=True, exist_ok=True)
    return d


def preview(conn: sqlite3.Connection, photo_id: int, spec: EditSpec, side: int = 1400) -> bytes:
    import io

    row, path = _source(conn, photo_id)
    if row["media_type"] != "image":
        raise EditError("only photos can be edited here")
    img = apply(rotate_image(imaging.decode(path, max_side=side).image, row["rotation"] or 0), spec)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def save_edit(ctx, conn: sqlite3.Connection, photo_id: int, spec: EditSpec) -> dict:
    row, path = _source(conn, photo_id)
    if row["media_type"] != "image":
        raise EditError("only photos can be edited here; videos can be trimmed")
    if spec.is_noop() and not row["rotation"]:
        raise EditError("nothing to save — no change was made")
    dec = imaging.decode(path, max_side=FULL_SIDE)
    img = apply(rotate_image(dec.image, row["rotation"] or 0), spec)
    exif = None
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            if ex:
                ex[0x0112] = 1                   # the pixels are upright now
                exif = ex.tobytes()
    except Exception:
        exif = None
    dest = _claim(_dest_dir(ctx, conn, row), f"{Path(row['filename']).stem} (edited).jpg")
    tmp = dest.with_name(dest.name + ".part")
    kw = {"quality": 92, "subsampling": 0}
    if exif:
        kw["exif"] = exif
    img.save(tmp, "JPEG", **kw)
    os.replace(tmp, dest)
    if row["mtime"]:
        try:
            os.utime(dest, (row["mtime"], row["mtime"]))
        except OSError:
            pass
    conn.execute("INSERT INTO edits(original_id, path, kind, created_at) VALUES (?,?,?,?)",
                 (photo_id, str(dest), "photo", time.time()))
    conn.commit()
    return {"path": str(dest), "width": img.width, "height": img.height}


def trim_video(ctx, conn: sqlite3.Connection, photo_id: int, start: float, end: float) -> dict:
    """Copy the packets between `start` and `end` seconds (from the key frame at or before start)."""
    import av

    row, path = _source(conn, photo_id)
    if row["media_type"] != "video":
        raise EditError("only videos can be trimmed")
    if not (0 <= start < end):
        raise EditError("the end must come after the start")
    ext = Path(row["filename"]).suffix.lower() or ".mp4"
    dest = _claim(_dest_dir(ctx, conn, row), f"{Path(row['filename']).stem} (trim){ext}")
    tmp = dest.with_name(dest.stem + ".part" + ext)
    src = av.open(str(path))
    try:
        vstreams = [s for s in src.streams if s.type in ("video", "audio")]
        if not vstreams:
            raise EditError("no video in this file")
        out = av.open(str(tmp), "w")
        try:
            mapping = {s.index: out.add_stream_from_template(s) for s in vstreams}
            video = next((s for s in vstreams if s.type == "video"), vstreams[0])
            src.seek(int(start / video.time_base), stream=video, backward=True, any_frame=False)
            offsets: dict[int, int] = {}
            copied = 0
            for pkt in src.demux(vstreams):
                if pkt.dts is None or pkt.pts is None:
                    continue
                t = float(pkt.pts * pkt.time_base)
                if t > end:
                    if pkt.stream.index == video.index:
                        break
                    continue
                if pkt.stream.index not in offsets:
                    offsets[pkt.stream.index] = pkt.dts
                off = offsets[pkt.stream.index]
                pkt.pts -= off
                pkt.dts -= off
                pkt.stream = mapping[pkt.stream.index]
                out.mux(pkt)
                copied += 1
        finally:
            out.close()
    finally:
        src.close()
    if not copied:
        tmp.unlink(missing_ok=True)
        raise EditError("nothing between those times")
    os.replace(tmp, dest)
    conn.execute("INSERT INTO edits(original_id, path, kind, created_at) VALUES (?,?,?,?)",
                 (photo_id, str(dest), "trim", time.time()))
    conn.commit()
    return {"path": str(dest)}
