"""EXIF / XMP / filename metadata extraction with explicit confidence."""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from PIL import Image

EXIF_IFD = 0x8769
GPS_IFD = 0x8825

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_SOFTWARE = 0x0131
TAG_DATETIME = 0x0132
TAG_DT_ORIGINAL = 0x9003
TAG_DT_DIGITIZED = 0x9004
TAG_OFFSET_ORIGINAL = 0x9011
TAG_OFFSET = 0x9010
TAG_EXPOSURE = 0x829A
TAG_FNUMBER = 0x829D
TAG_ISO = 0x8827
TAG_FOCAL = 0x920A
TAG_LENS_MAKE = 0xA433
TAG_LENS_MODEL = 0xA434
TAG_USER_COMMENT = 0x9286
TAG_PIXEL_X = 0xA002
TAG_PIXEL_Y = 0xA003

MIN_YEAR = 1985
PHONE_MAKES = (
    "apple", "samsung", "xiaomi", "redmi", "poco", "google", "oneplus", "oppo", "vivo", "realme",
    "motorola", "huawei", "honor", "nokia", "hmd", "nothing", "lg", "sony mobile", "asus", "lenovo",
    "iqoo", "infinix", "tecno", "micromax", "lava", "htc", "zte", "meizu",
)
CAMERA_MAKES = (
    "canon", "nikon", "sony", "fujifilm", "olympus", "om digital", "panasonic", "leica", "pentax",
    "ricoh", "hasselblad", "sigma", "gopro", "dji", "kodak", "casio", "minolta", "phase one",
)
EDIT_SOFTWARE = (
    "photoshop", "lightroom", "snapseed", "gimp", "picsart", "vsco", "facetune", "canva", "pixlr",
    "affinity", "capture one", "luminar", "darktable", "rawtherapee", "photos 1", "meitu", "lensa",
    "remini", "b612", "snow", "polarr", "afterlight", "instagram",
)


def _clean(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8", "ignore")
        except Exception:
            return None
    s = str(v).replace("\x00", "").strip()
    return s or None


def _float(v) -> float | None:
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1]) if v[1] else None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except Exception:
        return None


def parse_exif_datetime(s: str | None) -> datetime | None:
    s = _clean(s)
    if not s or s.startswith("0000"):
        return None
    m = _EXIF_DT_RE.match(s)
    if not m:
        return None
    y, mo, d, h, mi, sec = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0))
    except ValueError:
        return None
    return dt if _plausible(dt) else None


_EXIF_DT_RE = re.compile(r"\s*(\d{4})[:/-](\d{1,2})[:/-](\d{1,2})(?:[ T:]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def _plausible(dt: datetime) -> bool:
    return MIN_YEAR <= dt.year <= datetime.now().year + 1


def parse_offset(s: str | None) -> int | None:
    s = _clean(s)
    if not s:
        return None
    m = re.match(r"([+-])(\d{2}):?(\d{2})", s)
    if not m:
        return None
    mins = int(m.group(2)) * 60 + int(m.group(3))
    return mins if m.group(1) == "+" else -mins


def _gps_coord(values, ref) -> float | None:
    try:
        if values is None:
            return None
        if isinstance(values, (int, float)):
            deg = float(values)
        else:
            parts = [_float(v) for v in values]
            if any(p is None for p in parts):
                return None
            while len(parts) < 3:
                parts.append(0.0)
            deg = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
        ref = _clean(ref)
        if ref and ref.upper() in ("S", "W"):
            deg = -deg
        return deg
    except Exception:
        return None


def parse_gps(gps: dict) -> tuple[float | None, float | None, float | None]:
    if not gps:
        return None, None, None
    lat = _gps_coord(gps.get(2), gps.get(1))
    lon = _gps_coord(gps.get(4), gps.get(3))
    alt = _float(gps.get(6))
    if alt is not None and gps.get(5) in (1, b"\x01"):
        alt = -alt
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None, None, None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:  # "null island": phone wrote zeros with no fix
        return None, None, None
    return round(lat, 7), round(lon, 7), alt


_XMP_DATE_RE = re.compile(
    r"(?:exif:DateTimeOriginal|photoshop:DateCreated|xmp:CreateDate)\s*(?:=\s*\"|>)\s*"
    r"(\d{4}-\d{2}-\d{2}T?\s?\d{2}:\d{2}(?::\d{2})?)"
)


def parse_xmp_date(xmp) -> datetime | None:
    if not xmp:
        return None
    if isinstance(xmp, bytes):
        xmp = xmp.decode("utf-8", "ignore")
    m = _XMP_DATE_RE.search(str(xmp))
    if not m:
        return None
    s = m.group(1).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if _plausible(dt) else None
        except ValueError:
            continue
    return None


# ---- filename / folder date patterns ------------------------------------------------------

_SEP = r"[-_. ]?"
FILENAME_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # WhatsApp: IMG-20250812-WA0001 (date only, reflects send/receive day)
    (re.compile(r"(?:IMG|VID|PTT|STK)-(\d{4})(\d{2})(\d{2})-WA\d+", re.I), "date", "whatsapp"),
    # WhatsApp desktop/web: "WhatsApp Image 2025-08-12 at 14.30.12"
    (re.compile(r"WhatsApp Image (\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})(?:\s*([AP]M))?", re.I), "datetime", "whatsapp"),
    # Mac screenshots: "Screenshot 2025-08-12 at 14.30.12" / "Screen Shot 2020-01-01 at 1.02.03 PM"
    (re.compile(r"Screen ?[Ss]hot (\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})(?:\s*([AP]M))?", re.I), "datetime", "screenshot"),
    # Android screenshots: Screenshot_20250812-143012 / Screenshot_2025-08-12-14-30-12
    (re.compile(r"Screenshot[_-](\d{4})-?(\d{2})-?(\d{2})[-_](\d{2})-?(\d{2})-?(\d{2})", re.I), "datetime", "screenshot"),
    # Telegram: photo_2025-08-12_14-30-12
    (re.compile(r"photo_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", re.I), "datetime", "download"),
    # Signal: signal-2025-08-12-143012
    (re.compile(r"signal-(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})", re.I), "datetime", "download"),
    # Phones/cameras: IMG_20250812_143012, PXL_20250812_143012345, 20250812_143012, MVIMG_..., VID_...
    (re.compile(r"(?:^|[^0-9])(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"), "datetime", "phone"),
    # Dropbox camera uploads: 2025-08-12 14.30.12
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})\.(\d{2})\.(\d{2})"), "datetime", "phone"),
    # Generic ISO-ish datetime: 2025-08-12_14-30-12, 2025-08-12T14:30:12
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[T_ -](\d{2})[-:.](\d{2})[-:.](\d{2})"), "datetime", None),
    # Facebook / epoch milliseconds: FB_IMG_1565524312345, 1565524312345
    (re.compile(r"(?:FB_IMG_|received_|^)(1[2-9]\d{11})(?:\D|$)"), "epoch_ms", "download"),
    # Plain date: 2025-08-12, 20250812
    (re.compile(r"(?:^|[^0-9])(\d{4})-(\d{2})-(\d{2})(?:[^0-9]|$)"), "date", None),
    (re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})(\d{2})(\d{2})(?:[^0-9]|$)"), "date", None),
]

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9

_FOLDER_YMD = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})[-_. ]?(\d{2})[-_. ]?(\d{2})(?:[^0-9]|$)")
_FOLDER_YM = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})[-_. /](\d{1,2})(?:[^0-9]|$)")
_FOLDER_MONTH_YEAR = re.compile(r"\b([A-Za-z]{3,9})[\s_,.-]*((?:19|20)\d{2})\b")
_FOLDER_YEAR = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)")


def _mk(y, mo, d, h=12, mi=0, s=0, ampm=None) -> datetime | None:
    try:
        h = int(h)
        if ampm:
            ampm = ampm.upper()
            if ampm == "PM" and h < 12:
                h += 12
            if ampm == "AM" and h == 12:
                h = 0
        dt = datetime(int(y), int(mo), int(d), h, int(mi), int(s))
        return dt if _plausible(dt) else None
    except (ValueError, TypeError):
        return None


def date_from_filename(filename: str) -> tuple[datetime | None, str | None, str | None]:
    """Returns (datetime, precision 'datetime'|'date', source hint)."""
    stem = filename.rsplit(".", 1)[0]
    for pattern, kind, hint in FILENAME_PATTERNS:
        m = pattern.search(stem)
        if not m:
            continue
        g = m.groups()
        if kind == "epoch_ms":
            try:
                dt = datetime.fromtimestamp(int(g[0]) / 1000, tz=timezone.utc).replace(tzinfo=None)
            except (ValueError, OSError, OverflowError):
                continue
            if _plausible(dt):
                return dt, "datetime", hint
            continue
        if kind == "date":
            dt = _mk(g[0], g[1], g[2])
            if dt:
                return dt, "date", hint
            continue
        ampm = g[6] if len(g) > 6 else None
        dt = _mk(g[0], g[1], g[2], g[3], g[4], g[5], ampm)
        if dt:
            return dt, "datetime", hint
    return None, None, None


def date_from_folder(folder: str) -> tuple[datetime | None, str | None]:
    """Weak date hint from folder names like '2019/2019-08-12 Goa' or 'Wedding Jan 2026'."""
    parts = [p for p in PurePosixPath(folder).parts if p not in ("/", "")]
    for part in reversed(parts):
        m = _FOLDER_YMD.search(part)
        if m:
            dt = _mk(m.group(1), m.group(2), m.group(3))
            if dt:
                return dt, "date"
        for m in _FOLDER_MONTH_YEAR.finditer(part):
            mo = MONTHS.get(m.group(1).lower())
            if mo:
                dt = _mk(m.group(2), mo, 15)
                if dt:
                    return dt, "month"
        m = _FOLDER_YM.search(part)
        if m and 1 <= int(m.group(2)) <= 12:
            dt = _mk(m.group(1), m.group(2), 15)
            if dt:
                return dt, "month"
    # year-only: check nested "2019/..." structure from the leaf upward
    for part in reversed(parts):
        m = _FOLDER_YEAR.search(part)
        if m:
            dt = _mk(m.group(1), 7, 1)
            if dt:
                return dt, "year"
    return None, None


def naive_to_ts(dt: datetime) -> float:
    """Wall-clock datetime -> float seconds, treating it as UTC (see schema notes)."""
    return calendar.timegm(dt.timetuple()) + dt.microsecond / 1e6


def ts_to_naive(ts: float) -> datetime:
    return datetime(1970, 1, 1) + timedelta(seconds=ts)


# ---- main extraction ---------------------------------------------------------------------

def extract(exif: Image.Exif | None, xmp, filename: str, folder: str, mtime: float,
            ctime: float | None, fmt: str, width: int, height: int) -> dict:
    out: dict = {}
    ifd0: dict = {}
    exif_ifd: dict = {}
    gps: dict = {}
    if exif:
        try:
            ifd0 = dict(exif)
        except Exception:
            ifd0 = {}
        try:
            exif_ifd = dict(exif.get_ifd(EXIF_IFD))
        except Exception:
            exif_ifd = {}
        try:
            gps = dict(exif.get_ifd(GPS_IFD))
        except Exception:
            gps = {}

    make = _clean(ifd0.get(TAG_MAKE))
    model = _clean(ifd0.get(TAG_MODEL))
    if make and model and model.lower().startswith(make.lower()):
        model = model[len(make):].strip() or model
    out["camera_make"] = make
    out["camera_model"] = model
    out["software"] = _clean(ifd0.get(TAG_SOFTWARE))
    lens = _clean(exif_ifd.get(TAG_LENS_MODEL))
    lens_make = _clean(exif_ifd.get(TAG_LENS_MAKE))
    if lens and lens_make and not lens.lower().startswith(lens_make.lower()):
        lens = f"{lens_make} {lens}"
    out["lens"] = lens
    out["focal_length"] = _float(exif_ifd.get(TAG_FOCAL))
    out["aperture"] = _float(exif_ifd.get(TAG_FNUMBER))
    out["exposure_time"] = _float(exif_ifd.get(TAG_EXPOSURE))
    iso = exif_ifd.get(TAG_ISO)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    try:
        out["iso"] = int(iso) if iso is not None else None
    except (TypeError, ValueError):
        out["iso"] = None

    lat, lon, alt = parse_gps(gps)
    out["gps_lat"], out["gps_lon"], out["gps_alt"] = lat, lon, alt

    # ---- capture date, most to least trustworthy
    fn_dt, fn_precision, fn_hint = date_from_filename(filename)
    dt = parse_exif_datetime(exif_ifd.get(TAG_DT_ORIGINAL))
    source, conf = ("exif", "high") if dt else (None, None)
    if dt is None:
        dt = parse_exif_datetime(exif_ifd.get(TAG_DT_DIGITIZED))
        if dt:
            source, conf = "exif_digitized", "high"
    if dt is None:
        dt = parse_xmp_date(xmp)
        if dt:
            source, conf = "xmp", "high"
    if dt is None and fn_dt is not None and fn_precision == "datetime":
        dt, source, conf = fn_dt, "filename", "high" if fn_hint in ("phone", "screenshot") else "medium"
    if dt is None:
        d0 = parse_exif_datetime(ifd0.get(TAG_DATETIME))  # often the *edit* time
        if d0:
            dt, source, conf = d0, "exif_modified", "medium"
    if dt is None and fn_dt is not None:
        dt, source, conf = fn_dt, "filename", "medium"
    if dt is None:
        fdt, fprec = date_from_folder(folder)
        if fdt is not None and fprec in ("date", "month"):
            dt, source, conf = fdt, "folder", "low"
    if dt is None:
        # File times: mtime survives copies on most systems; ctime on Windows is creation time.
        cands = [t for t in (mtime, ctime) if t]
        t = min(cands) if cands else mtime
        dt = datetime.fromtimestamp(t)
        source, conf = "mtime", "low"

    tz = parse_offset(exif_ifd.get(TAG_OFFSET_ORIGINAL)) or parse_offset(exif_ifd.get(TAG_OFFSET))
    out["taken_ts"] = naive_to_ts(dt)
    out["taken_local"] = dt.strftime("%Y-%m-%d %H:%M:%S")
    out["tz_offset_min"] = tz
    out["date_source"] = source
    out["date_confidence"] = conf
    out["source_kind"] = classify_source(filename, folder, make, out["software"], fmt, width, height,
                                         bool(exif_ifd), fn_hint)
    return out


_SCREEN_ASPECTS = (16 / 9, 9 / 16, 19.5 / 9, 9 / 19.5, 20 / 9, 9 / 20, 18 / 9, 9 / 18, 16 / 10, 10 / 16,
                   19 / 9, 9 / 19, 21 / 9, 9 / 21, 3 / 2, 2 / 3)
_COMMON_SCREEN_WIDTHS = {720, 750, 828, 1080, 1125, 1170, 1179, 1242, 1284, 1290, 1440, 1366, 1536,
                         1600, 1920, 2160, 2560, 2880, 3840, 1600, 900, 768, 1280}


def classify_source(filename: str, folder: str, make: str | None, software: str | None, fmt: str,
                    width: int, height: int, has_exif_ifd: bool, fn_hint: str | None) -> str:
    fl = filename.lower()
    fol = folder.lower()
    if "screenshot" in fl or "screen shot" in fl or "screenshots" in fol or fn_hint == "screenshot":
        return "screenshot"
    if fn_hint == "whatsapp" or "whatsapp" in fol or "whatsapp" in fl:
        return "whatsapp"
    sw = (software or "").lower()
    if any(k in sw for k in EDIT_SOFTWARE) or re.search(r"(?:[-_ ](?:edit(?:ed)?|copy)\b|\(\d+\)$)", fl.rsplit(".", 1)[0]):
        if make:
            return "edited"
    mk = (make or "").lower()
    if mk:
        if any(mk.startswith(p) or p in mk for p in PHONE_MAKES):
            return "phone"
        if any(mk.startswith(c) or c in mk for c in CAMERA_MAKES):
            return "camera"
        return "camera"
    if any(k in fol for k in ("download", "telegram", "instagram", "facebook", "saved pictures", "pinterest")) or fn_hint == "download":
        return "download"
    if "scan" in fl or "scan" in fol:
        return "scan"
    if fmt == "PNG" and not has_exif_ifd and width and height:
        short, long_ = sorted((width, height))
        if short in _COMMON_SCREEN_WIDTHS and any(abs(long_ / short - a) < 0.02 for a in _SCREEN_ASPECTS if a >= 1):
            return "screenshot"
    if fn_hint == "phone":
        return "phone"
    return "unknown"
