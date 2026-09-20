"""Robust, read-only image decoding for every supported format.

Originals are never written. Decoding uses the cheapest path that yields the
resolution we need (JPEG DCT-domain downscaling, RAW embedded previews).
"""
from __future__ import annotations

import io
import os
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps

from .config import RAW_EXTENSIONS

# Accept truncated files where possible (partial download / sync) — we log them via `truncated`.
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 400_000_000  # allow panoramas, still guard against decompression bombs
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

try:  # HEIC / HEIF / AVIF
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:  # pragma: no cover
    HEIF_AVAILABLE = False

try:
    import rawpy

    RAW_AVAILABLE = True
except Exception:  # pragma: no cover
    RAW_AVAILABLE = False

BROWSER_NATIVE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


class DecodeError(Exception):
    pass


@dataclass
class Decoded:
    image: Image.Image            # RGB, orientation applied, downscaled to <= requested long side
    orig_width: int               # oriented original dimensions
    orig_height: int
    orientation: int
    format: str
    exif: Image.Exif | None
    xmp: bytes | str | None
    info: dict


_TRANSPOSE_SWAPS = {5, 6, 7, 8}


def _raw_flip_to_orientation(flip: int) -> int:
    return {0: 1, 3: 3, 5: 8, 6: 6}.get(flip, 1)


def _decode_raw(path: Path, max_side: int) -> Decoded:
    if not RAW_AVAILABLE:
        raise DecodeError("rawpy not available for RAW decoding")
    exif = None
    try:  # most RAW formats are TIFF containers; Pillow can often read their EXIF header
        with Image.open(path) as tiff_hdr:
            exif = tiff_hdr.getexif()
    except Exception:
        pass
    with rawpy.imread(str(path)) as raw:
        orientation = _raw_flip_to_orientation(raw.sizes.flip)
        full_w, full_h = raw.sizes.width, raw.sizes.height
        img = None
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
                img.draft("RGB", (max_side, max_side))
                img = img.convert("RGB")
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                img = Image.fromarray(thumb.data)
            # Tiny previews are useless for faces; fall back to demosaicing.
            if img is not None and max(img.size) < min(1024, max_side):
                img = None
        except Exception:
            img = None
        if img is None:
            rgb = raw.postprocess(use_camera_wb=True, half_size=True, no_auto_bright=False, output_bps=8)
            img = Image.fromarray(rgb)
            orientation = 1  # postprocess already applies flip
    if orientation != 1:
        img = _apply_orientation(img, orientation)
    if orientation in _TRANSPOSE_SWAPS:
        full_w, full_h = full_h, full_w
    img.thumbnail((max_side, max_side), Image.Resampling.BILINEAR, reducing_gap=2.0)
    return Decoded(img, full_w, full_h, orientation, "RAW", exif, None, {})


def _apply_orientation(img: Image.Image, orientation: int) -> Image.Image:
    method = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }.get(orientation)
    return img.transpose(method) if method is not None else img


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    if img.mode in ("I;16", "I;16B", "I;16L", "I"):
        arr = np.asarray(img, dtype=np.float32)
        hi = float(arr.max()) or 1.0
        return Image.fromarray((arr / hi * 255).clip(0, 255).astype(np.uint8)).convert("RGB")
    return img.convert("RGB")


def decode(path: Path | str, max_side: int = 1600, data: bytes | None = None) -> Decoded:
    """Decode an image to RGB with orientation applied and long side <= max_side."""
    path = Path(path)
    if path.suffix.lower() in RAW_EXTENSIONS:
        try:
            return _decode_raw(path, max_side)
        except DecodeError:
            raise
        except Exception as exc:
            raise DecodeError(f"RAW decode failed: {exc}") from exc
    try:
        src = io.BytesIO(data) if data is not None else path
        img = Image.open(src)
        fmt = img.format or path.suffix.lstrip(".").upper()
        try:
            exif = img.getexif()
        except Exception:
            exif = None
        orientation = 1
        if exif:
            try:
                orientation = int(exif.get(0x0112, 1) or 1)
            except Exception:
                orientation = 1
            if orientation not in range(1, 9):
                orientation = 1
        xmp = img.info.get("xmp") or img.info.get("XML:com.adobe.xmp")
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        raw_w, raw_h = img.size
        if fmt == "JPEG":
            # DCT-domain downscale: request the oriented target box in *stored* orientation.
            scale = max_side / max(raw_w, raw_h)
            if scale < 1:
                img.draft("RGB", (max(1, int(raw_w * scale)), max(1, int(raw_h * scale))))
        img.load()
        info = dict(img.info)
        img = _to_rgb(img)
        if orientation != 1:
            img = _apply_orientation(img, orientation)
        ow, oh = (raw_h, raw_w) if orientation in _TRANSPOSE_SWAPS else (raw_w, raw_h)
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.Resampling.BILINEAR, reducing_gap=2.0)
        return Decoded(img, ow, oh, orientation, fmt, exif, xmp, info)
    except DecodeError:
        raise
    except Exception as exc:
        raise DecodeError(f"{type(exc).__name__}: {exc}") from exc


def save_thumbnail(img: Image.Image, dest: Path, long_side: int, quality: int = 78) -> tuple[int, int]:
    thumb = img.copy()
    thumb.thumbnail((long_side, long_side), Image.Resampling.LANCZOS, reducing_gap=3.0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Duplicate photos share a content-hash filename, so two workers can race here.
    tmp = dest.with_suffix(f"{dest.suffix}.{os.getpid()}-{threading.get_ident():x}.tmp")
    fmt = "WEBP" if dest.suffix.lower() == ".webp" else "JPEG"
    # WebP method>0 costs 3-5x the CPU for ~20% smaller files — not worth it at library scale.
    kwargs = {"quality": quality, "method": 0} if fmt == "WEBP" else {"quality": quality, "progressive": True}
    thumb.save(tmp, fmt, **kwargs)
    try:
        os.replace(tmp, dest)
    except OSError:  # another worker won the race; its file is equivalent
        tmp.unlink(missing_ok=True)
    return thumb.size


def thumb_path(thumb_root: Path, sha256: str, ext: str = ".webp") -> Path:
    return thumb_root / sha256[:2] / f"{sha256}{ext}"


def pil_to_np(img: Image.Image) -> np.ndarray:
    return np.asarray(img, dtype=np.uint8)
