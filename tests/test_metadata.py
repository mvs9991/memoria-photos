"""Metadata extraction: dates, GPS, camera, provenance."""
from datetime import datetime

import pytest

from photointel import metadata as md


@pytest.mark.parametrize("filename,expected,precision", [
    ("IMG_20250812_143012.jpg", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("PXL_20250812_143012345.jpg", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("20250812_143012.jpg", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("IMG-20250812-WA0001.jpg", datetime(2025, 8, 12, 12, 0), "date"),
    ("WhatsApp Image 2025-08-12 at 14.30.12.jpeg", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("Screenshot_20250812-143012.png", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("Screenshot 2025-08-12 at 14.30.12.png", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("Screen Shot 2020-01-01 at 1.02.03 PM.png", datetime(2020, 1, 1, 13, 2, 3), "datetime"),
    ("photo_2025-08-12_14-30-12.jpg", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("signal-2025-08-12-143012.jpg", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("2025-08-12 14.30.12.jpg", datetime(2025, 8, 12, 14, 30, 12), "datetime"),
    ("2019-04-05.jpg", datetime(2019, 4, 5, 12, 0), "date"),
])
def test_filename_dates(filename, expected, precision):
    dt, prec, _ = md.date_from_filename(filename)
    assert dt == expected
    assert prec == precision


@pytest.mark.parametrize("filename", ["holiday.jpg", "DSC_0041.JPG", "image (3).png", "99999999_x.jpg"])
def test_filename_without_date(filename):
    dt, _, _ = md.date_from_filename(filename)
    assert dt is None


def test_filename_epoch_millis():
    dt, prec, hint = md.date_from_filename("FB_IMG_1565524312345.jpg")
    assert dt is not None and dt.year == 2019
    assert hint == "download"


def test_implausible_dates_rejected():
    assert md.date_from_filename("IMG_18000101_120000.jpg")[0] is None
    assert md.parse_exif_datetime("0000:00:00 00:00:00") is None
    assert md.parse_exif_datetime("2040:01:01 10:00:00") is None  # beyond next year


def test_folder_dates():
    dt, prec = md.date_from_folder("Pictures/2019-08-12 Goa")
    assert dt and (dt.year, dt.month, dt.day) == (2019, 8, 12) and prec == "date"
    dt, prec = md.date_from_folder("Wedding Jan 2026")
    assert dt and (dt.year, dt.month) == (2026, 1) and prec == "month"
    dt, prec = md.date_from_folder("Albums/2015")
    assert dt and dt.year == 2015 and prec == "year"
    assert md.date_from_folder("Camera")[0] is None


def test_gps_parsing_and_null_island():
    gps = {1: "N", 2: ((17, 1), (23, 1), (600, 100)), 3: "E", 4: ((78, 1), (29, 1), (1200, 100))}
    lat, lon, _ = md.parse_gps(gps)
    assert lat == pytest.approx(17.3850, abs=1e-3)
    assert lon == pytest.approx(78.4867, abs=1e-3)
    zero = {1: "N", 2: ((0, 1), (0, 1), (0, 1)), 3: "E", 4: ((0, 1), (0, 1), (0, 1))}
    assert md.parse_gps(zero) == (None, None, None)
    assert md.parse_gps({}) == (None, None, None)
    bad = {1: "N", 2: ((200, 1), (0, 1), (0, 1)), 3: "E", 4: ((0, 1), (0, 1), (0, 1))}
    assert md.parse_gps(bad) == (None, None, None)


def test_south_west_hemisphere():
    gps = {1: "S", 2: ((33, 1), (52, 1), (0, 1)), 3: "W", 4: ((70, 1), (40, 1), (0, 1))}
    lat, lon, _ = md.parse_gps(gps)
    assert lat < 0 and lon < 0


@pytest.mark.parametrize("filename,folder,make,software,fmt,expected", [
    ("IMG_20240101_101010.jpg", "DCIM/Camera", "samsung", None, "JPEG", "phone"),
    ("DSC_0001.JPG", "Photos", "NIKON CORPORATION", None, "JPEG", "camera"),
    ("IMG-20240101-WA0001.jpg", "WhatsApp/Media", None, None, "JPEG", "whatsapp"),
    ("Screenshot_20240101-101010.png", "Pictures", None, None, "PNG", "screenshot"),
    ("photo.jpg", "Downloads", None, None, "JPEG", "download"),
    ("IMG_1234-edited.jpg", "Pictures", "Apple", "Snapseed 2.19", "JPEG", "edited"),
])
def test_source_classification(filename, folder, make, software, fmt, expected):
    hint = md.date_from_filename(filename)[2]
    got = md.classify_source(filename, folder, make, software, fmt, 4000, 3000, bool(make), hint)
    assert got == expected


def test_timestamp_roundtrip():
    dt = datetime(2024, 5, 17, 19, 30, 15)
    assert md.ts_to_naive(md.naive_to_ts(dt)) == dt


def test_offset_parsing():
    assert md.parse_offset("+05:30") == 330
    assert md.parse_offset("-08:00") == -480
    assert md.parse_offset("garbage") is None


def test_device_env_override(tmp_path, monkeypatch):
    """PHOTOINTEL_DEVICE overrides the saved device without rewriting settings."""
    from photointel.config import Settings

    s = Settings()
    s.device = "cuda"
    s.save(tmp_path)

    monkeypatch.setenv("PHOTOINTEL_DEVICE", "cpu")
    assert Settings.load(tmp_path).device == "cpu"

    monkeypatch.setenv("PHOTOINTEL_DEVICE", "nonsense")
    assert Settings.load(tmp_path).device == "cuda"  # invalid value ignored

    monkeypatch.delenv("PHOTOINTEL_DEVICE")
    assert Settings.load(tmp_path).device == "cuda"


def test_shared_model_and_geo_dirs(tmp_path, monkeypatch):
    """Weights are machine-level: a second library can point at the same copy."""
    from photointel.config import Paths

    shared = tmp_path / "shared"
    monkeypatch.setenv("PHOTOINTEL_MODELS", str(shared / "models"))
    monkeypatch.setenv("PHOTOINTEL_GEO", str(shared / "geo"))
    p = Paths(tmp_path / "libA")
    assert p.models == (shared / "models").resolve()
    assert p.geo == (shared / "geo").resolve()
    assert p.db == (tmp_path / "libA" / "library.db").resolve()   # library data stays put

    monkeypatch.delenv("PHOTOINTEL_MODELS")
    monkeypatch.delenv("PHOTOINTEL_GEO")
    q = Paths(tmp_path / "libB")
    assert q.models == (tmp_path / "libB" / "models").resolve()
