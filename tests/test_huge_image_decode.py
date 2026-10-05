"""A huge image is shrunk before its full-size colour conversion (2.2 GB -> 0.6 GB peak for 400 MP, measured)."""
from __future__ import annotations

from PIL import Image

from photointel import imaging


def test_a_huge_image_is_not_converted_at_full_size(tmp_path, monkeypatch):
    p = tmp_path / "pano.png"
    Image.new("L", (9000, 8000), 128).save(p)            # 72 MP greyscale
    seen = []
    real = imaging._to_rgb
    monkeypatch.setattr(imaging, "_to_rgb", lambda img: (seen.append(img.size), real(img))[1])
    d = imaging.decode(p, 1600)
    assert max(d.image.size) == 1600 and (d.orig_width, d.orig_height) == (9000, 8000)
    assert max(seen[0]) <= 4800, seen                     # was (9000, 8000): a full-size RGB copy


def test_an_ordinary_photo_decodes_as_before(tmp_path, monkeypatch):
    p = tmp_path / "photo.png"
    Image.new("RGB", (4032, 3024), (10, 20, 30)).save(p)  # 12 MP: untouched by the huge-image path
    seen = []
    real = imaging._to_rgb
    monkeypatch.setattr(imaging, "_to_rgb", lambda img: (seen.append(img.size), real(img))[1])
    imaging.decode(p, 1600)
    assert seen == [(4032, 3024)]


def test_a_gps_minute_or_second_of_60_or_more_is_corrupt_not_a_position():
    from photointel.metadata import parse_gps

    assert parse_gps({1: "N", 2: (17, 90, 0), 3: "E", 4: (78, 28, 0)}) == (None, None, None)   # was 18.5 N
    assert parse_gps({1: "N", 2: (17, 23, 61), 3: "E", 4: (78, 28, 0)}) == (None, None, None)
    lat, lon, _ = parse_gps({1: "N", 2: (17, 23.5, 0), 3: "E", 4: (78, 28, 59.9)})          # decimal minutes are fine
    assert round(lat, 4) == 17.3917 and round(lon, 4) == 78.4833
