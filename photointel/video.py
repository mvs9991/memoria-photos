"""Video, Live photo and motion photo support (read-only, via PyAV's bundled FFmpeg).

A video is indexed like a photo: one representative frame goes through the same
thumbnail / face / semantic / quality pipeline, so videos are searchable by who and
what is in them. Container metadata supplies the capture date, GPS and camera.

Motion photos (Google "MVIMG", Pixel, Samsung) are a JPEG with an MP4 appended after
it. Only the byte offset is recorded; the video is served straight out of the original
file, which is never modified.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

try:
    import av

    AV_AVAILABLE = True
except Exception:  # pragma: no cover
    AV_AVAILABLE = False

# Codecs every mainstream browser plays inside an MP4. Anything else (HEVC from
# iPhones, MPEG-2 from camcorders, WMV...) is transcoded to H.264 once, on demand.
BROWSER_CODECS = {"h264", "vp8", "vp9", "av1"}
BROWSER_CONTAINERS = {".mp4", ".m4v", ".mov", ".webm"}
LIVE_MAX_SECONDS = 5.0          # iPhone Live photo motion is ~3 s
MIN_YEAR = 1985


class VideoError(Exception):
    pass


@dataclass
class VideoInfo:
    width: int = 0                  # display (rotation applied) dimensions
    height: int = 0
    duration: float | None = None
    codec: str | None = None
    rotation: int = 0               # display rotation, degrees counter-clockwise
    taken: datetime | None = None   # wall-clock capture time
    taken_confidence: str | None = None
    taken_source: str | None = None
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    make: str | None = None
    model: str | None = None
    software: str | None = None
    tags: dict = field(default_factory=dict)


_ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?")


def parse_iso6709(value: str | None) -> tuple[float | None, float | None, float | None]:
    """'+17.3850+078.4867+500.0/' -> (17.385, 78.4867, 500.0). Decimal-degree form only."""
    if not value:
        return None, None, None
    m = _ISO6709.search(value.strip())
    if not m:
        return None, None, None
    lat, lon = float(m.group(1)), float(m.group(2))
    alt = float(m.group(3)) if m.group(3) else None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
        return None, None, None
    return round(lat, 7), round(lon, 7), alt


def _plausible(dt: datetime) -> bool:
    return MIN_YEAR <= dt.year <= datetime.now().year + 1


_DT = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def parse_quicktime_date(value: str | None) -> datetime | None:
    """Apple's com.apple.quicktime.creationdate is local time *with* its offset.

    '2024-03-09T11:00:00+0530' -> 2024-03-09 11:00:00, the wall clock where it was shot.
    """
    m = _DT.match((value or "").strip())
    if not m:
        return None
    try:
        dt = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    return dt if _plausible(dt) else None


def parse_utc_creation_time(value: str | None) -> datetime | None:
    """MP4 'creation_time' is UTC with no offset recorded. It is converted with this
    machine's timezone, which is right whenever the video was shot where you live (and
    is what Windows Explorer does) — hence only medium confidence."""
    m = _DT.match((value or "").strip())
    if not m:
        return None
    try:
        utc = datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None
    if not _plausible(utc):  # Android often writes 1904-01-01 / 1970-01-01 placeholders
        return None
    return utc.astimezone().replace(tzinfo=None)


def _tag(tags: dict, *keys: str) -> str | None:
    lower = {k.lower(): v for k, v in tags.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, ""):
            return str(v).strip()
    return None


def _info_from_container(c) -> VideoInfo:
    if not c.streams.video:
        raise VideoError("no video stream")
    v = c.streams.video[0]
    tags = dict(c.metadata or {})
    tags.update({f"stream:{k}": val for k, val in (v.metadata or {}).items()})
    info = VideoInfo(tags=tags)
    info.codec = v.codec_context.name if v.codec_context else None
    info.width = int(v.codec_context.width or v.width or 0)
    info.height = int(v.codec_context.height or v.height or 0)
    if c.duration:
        info.duration = round(c.duration / 1_000_000, 3)
    elif v.duration and v.time_base:
        info.duration = round(float(v.duration * v.time_base), 3)

    qt = parse_quicktime_date(_tag(tags, "com.apple.quicktime.creationdate"))
    if qt:
        info.taken, info.taken_source, info.taken_confidence = qt, "video_meta", "high"
    else:
        utc = parse_utc_creation_time(_tag(tags, "creation_time", "stream:creation_time"))
        if utc:
            info.taken, info.taken_source, info.taken_confidence = utc, "video_meta_utc", "medium"
    info.lat, info.lon, info.alt = parse_iso6709(
        _tag(tags, "com.apple.quicktime.location.ISO6709", "location", "location-eng"))
    info.make = _tag(tags, "com.apple.quicktime.make", "com.android.manufacturer", "make")
    info.model = _tag(tags, "com.apple.quicktime.model", "com.android.model", "model")
    info.software = _tag(tags, "com.apple.quicktime.software", "com.android.version")
    return info


def upright(img: Image.Image, rotation: int) -> Image.Image:
    """Apply a display rotation (degrees counter-clockwise, FFmpeg display-matrix convention)."""
    return {90: lambda: img.transpose(Image.Transpose.ROTATE_90),
            180: lambda: img.transpose(Image.Transpose.ROTATE_180),
            270: lambda: img.transpose(Image.Transpose.ROTATE_270)}.get(rotation % 360, lambda: img)()


def representative_frame(path: Path | str, max_side: int = 1600) -> tuple[Image.Image, VideoInfo]:
    """A frame ~10% in (skips black lead-in and fades), upright, downscaled; plus metadata."""
    if not AV_AVAILABLE:
        raise VideoError("PyAV is not installed (pip install av)")
    try:
        with av.open(str(path)) as c:
            info = _info_from_container(c)
            v = c.streams.video[0]
            v.thread_type = "AUTO"
            target = (info.duration or 0) * 0.1
            if target > 0.5 and v.time_base:
                try:
                    c.seek(int(target / v.time_base), stream=v, backward=True)
                except Exception:
                    c.seek(0)
            frame = None
            for frame in c.decode(video=0):
                if frame.time is None or frame.time >= target - 0.05:
                    break
            if frame is None:
                raise VideoError("no decodable frames")
            info.rotation = int(getattr(frame, "rotation", 0) or 0) % 360
            img = upright(frame.to_image(), info.rotation)
    except VideoError:
        raise
    except Exception as exc:
        raise VideoError(f"{type(exc).__name__}: {exc}") from exc
    if info.rotation in (90, 270):
        info.width, info.height = info.height, info.width
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.BILINEAR, reducing_gap=2.0)
    return img.convert("RGB"), info


# ---- motion photos --------------------------------------------------------------------------

_MOTION_XMP_HINTS = (b"MicroVideo", b"MotionPhoto", b"Container:Directory")
_SAMSUNG_MARKER = b"MotionPhoto_Data"


def find_motion_offset(data: bytes | None) -> int | None:
    """Byte offset of an MP4 embedded after a JPEG's image data, or None.

    Requires *both* a motion-photo declaration (XMP hint or Samsung trailer marker) and
    a well-formed ISO-BMFF 'ftyp' box after it, so an ordinary JPEG that happens to
    contain those bytes is not misread as a video.
    """
    if not data or data[:2] != b"\xff\xd8":
        return None
    declared = any(h in data[: 256 * 1024] for h in _MOTION_XMP_HINTS)
    samsung = data.rfind(_SAMSUNG_MARKER)
    if not declared and samsung < 0:
        return None
    start = samsung + len(_SAMSUNG_MARKER) if samsung >= 0 else 4
    idx = data.find(b"ftyp", start)
    while idx >= 4:
        box_size = int.from_bytes(data[idx - 4: idx], "big")
        brand = data[idx + 4: idx + 8]
        if 8 <= box_size <= 64 and len(brand) == 4 and all(32 < b < 127 for b in brand):
            return idx - 4
        idx = data.find(b"ftyp", idx + 4)
    return None


def motion_bytes(path: Path | str, offset: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read()


# ---- browser playback ------------------------------------------------------------------------

def playable_in_browser(codec: str | None, ext: str) -> bool:
    return (codec or "").lower() in BROWSER_CODECS and ext.lower() in BROWSER_CONTAINERS


def transcode_to_mp4(src: Path | str, dest: Path, max_side: int = 1280) -> Path:
    """A browser-playable H.264 copy in the cache. Video only: a preview, not an archive."""
    if not AV_AVAILABLE:
        raise VideoError("PyAV is not installed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".part.mp4")
    try:
        with av.open(str(src)) as inp, av.open(str(tmp), "w", options={"movflags": "faststart"}) as out:
            vin = inp.streams.video[0]
            vin.thread_type = "AUTO"
            vout = None
            for frame in inp.decode(video=0):
                img = upright(frame.to_image(), int(getattr(frame, "rotation", 0) or 0))
                if max(img.size) > max_side:
                    img.thumbnail((max_side, max_side))
                w, h = img.width - img.width % 2, img.height - img.height % 2   # yuv420p needs even sizes
                if vout is None:
                    vout = out.add_stream("libx264", rate=vin.average_rate or 30)
                    vout.width, vout.height, vout.pix_fmt = w, h, "yuv420p"
                    vout.options = {"preset": "veryfast", "crf": "24"}
                if img.size != (vout.width, vout.height):
                    img = img.resize((vout.width, vout.height))
                for pkt in vout.encode(av.VideoFrame.from_image(img)):
                    out.mux(pkt)
            if vout is None:
                raise VideoError("no frames to transcode")
            for pkt in vout.encode():
                out.mux(pkt)
        tmp.replace(dest)
        return dest
    except VideoError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise VideoError(f"transcode failed: {exc}") from exc
