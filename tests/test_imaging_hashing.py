"""Decoding, thumbnails, hashing and quality metrics."""
import numpy as np
import pytest
from PIL import Image

from photointel import hashing, imaging, quality
from tests.conftest import make_image


def test_decode_formats(tmp_path):
    for fmt, ext in [("JPEG", "jpg"), ("PNG", "png"), ("WEBP", "webp"), ("TIFF", "tiff"), ("BMP", "bmp")]:
        p = make_image(tmp_path / f"x.{ext}", size=(400, 300), fmt=fmt)
        dec = imaging.decode(p, max_side=256)
        assert dec.image.mode == "RGB"
        assert max(dec.image.size) <= 256
        assert (dec.orig_width, dec.orig_height) == (400, 300)


def test_decode_downscales_but_reports_original_size(tmp_path):
    p = make_image(tmp_path / "big.jpg", size=(4000, 3000))
    dec = imaging.decode(p, max_side=800)
    assert max(dec.image.size) <= 800
    assert (dec.orig_width, dec.orig_height) == (4000, 3000)


def test_orientation_applied(tmp_path):
    p = make_image(tmp_path / "rot.jpg", size=(400, 300), orientation=6)
    dec = imaging.decode(p, max_side=4000)
    # orientation 6 rotates 90 degrees, so the reported size is swapped
    assert (dec.orig_width, dec.orig_height) == (300, 400)
    assert dec.image.size == (300, 400)


def test_decode_rejects_garbage(tmp_path):
    p = tmp_path / "bad.jpg"
    p.write_bytes(b"definitely not an image")
    with pytest.raises(imaging.DecodeError):
        imaging.decode(p)


def test_decode_truncated_jpeg_still_works(tmp_path):
    p = make_image(tmp_path / "full.jpg", size=(600, 400))
    data = p.read_bytes()
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(data[: int(len(data) * 0.6)])
    dec = imaging.decode(cut)          # truncated files are recoverable, not fatal
    assert dec.image.size[0] > 0


def test_transparency_composited(tmp_path):
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 0))
    p = tmp_path / "alpha.png"
    img.save(p)
    dec = imaging.decode(p)
    assert dec.image.mode == "RGB"
    assert dec.image.getpixel((5, 5)) == (255, 255, 255)   # composited onto white


def test_thumbnail_written_atomically(tmp_path):
    p = make_image(tmp_path / "src.jpg", size=(1200, 900))
    img = imaging.decode(p).image
    dest = tmp_path / "thumbs" / "ab" / "abc.webp"
    imaging.save_thumbnail(img, dest, 256)
    assert dest.exists()
    assert not list(dest.parent.glob("*.tmp"))
    assert max(Image.open(dest).size) <= 256


def test_hashes_are_stable_and_similar_for_resizes(tmp_path):
    p = make_image(tmp_path / "a.jpg", size=(800, 600), noise=8)
    img = imaging.decode(p).image
    h1 = hashing.perceptual_hashes(img)
    h2 = hashing.perceptual_hashes(img.copy())
    assert h1 == h2
    small = img.resize((400, 300))
    h3 = hashing.perceptual_hashes(small)
    assert hashing.hamming(h1[0], h3[0]) <= 6          # a resize stays close
    # A genuinely different *structure*, not just a different colour: pHash works on
    # luminance, so recolouring the same layout is supposed to stay close.
    arr = np.zeros((600, 800, 3), np.uint8)
    arr[:, ::2] = 255                                  # vertical stripes
    arr[400:, :] = 90
    Image.fromarray(arr).save(tmp_path / "b.jpg", "JPEG", quality=90)
    other = imaging.decode(tmp_path / "b.jpg").image
    h4 = hashing.perceptual_hashes(other)
    assert hashing.hamming(h1[0], h4[0]) > 8           # a different photo is far


def test_hamming_many_matches_scalar():
    ref = hashing._to_signed64(0b1011)
    arr = np.array([hashing._to_signed64(0b1011), hashing._to_signed64(0b1111),
                    hashing._to_signed64(0)], dtype=np.int64)
    got = hashing.hamming_many(ref, arr)
    assert list(got) == [0, 1, 3]


def test_sha256_file_and_bytes_agree(tmp_path):
    p = make_image(tmp_path / "x.jpg")
    assert hashing.sha256_file(p) == hashing.sha256_bytes(p.read_bytes())


def test_quality_metrics_discriminate(tmp_path):
    sharp = imaging.decode(make_image(tmp_path / "s.jpg", size=(600, 400), noise=60)).image
    flat = imaging.decode(make_image(tmp_path / "f.jpg", size=(600, 400), colour=(128, 128, 128))).image
    qs = quality.image_quality(sharp)
    qf = quality.image_quality(flat)
    assert qs["blur"] > qf["blur"]
    assert quality.sharpness_score(qs["blur"]) >= quality.sharpness_score(qf["blur"])


def test_exposure_score_prefers_midtones():
    mid = quality.exposure_score(0.47, 0.0, 0.2)
    dark = quality.exposure_score(0.03, 0.4, 0.02)
    assert mid > dark


def test_combined_quality_penalises_screenshots():
    row = {"blur": 300, "brightness": 0.5, "contrast": 0.2, "clipped": 0.01, "width": 4000, "height": 3000}
    normal = quality.combined_quality({**row, "source_kind": "camera"})
    shot = quality.combined_quality({**row, "source_kind": "screenshot"})
    assert shot < normal


def _write_heic(path, size=(800, 600)):
    """A real HEIC file. Returns None when the codec is unavailable."""
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    arr = np.zeros((size[1], size[0], 3), np.uint8)
    arr[:, ::3] = 200
    arr[size[1] // 2:, :] = 60
    Image.fromarray(arr).save(path, format="HEIF", quality=80)
    return path


def test_heic_decodes(tmp_path):
    """iPhone libraries are mostly HEIC; the rest of the suite only covers JPEG/PNG."""
    p = _write_heic(tmp_path / "IMG_0001.heic")
    dec = imaging.decode(p, max_side=256)
    assert dec.image.mode == "RGB"
    assert (dec.orig_width, dec.orig_height) == (800, 600)
    assert max(dec.image.size) <= 256


def test_heic_hashes_and_quality_work(tmp_path):
    p = _write_heic(tmp_path / "a.heic")
    img = imaging.decode(p).image
    ph, dh = hashing.perceptual_hashes(img)
    assert isinstance(ph, int) and isinstance(dh, int)
    q = quality.image_quality(img)
    assert q["blur"] > 0


def test_unreadable_raw_raises_decode_error_not_a_crash(tmp_path):
    """RAW decoding is best-effort; a file libraw cannot open must not escape as
    an arbitrary exception, or one bad card dump would abort a whole run."""
    p = tmp_path / "IMG_0001.dng"
    p.write_bytes(b"II*\x00" + b"\x00" * 512)      # TIFF magic, no valid RAW payload
    with pytest.raises(imaging.DecodeError):
        imaging.decode(p)
