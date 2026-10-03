"""Hostile and malformed files must never take the index down with them.

A real library is full of junk: half-synced files, renamed formats, camera
firmware bugs that write impossible dates, filenames from a dozen locales. The
property every one of these tests asserts is the same, and it is the one that
matters at scale — a bad file is recorded as an error beside the others, the run
finishes, and every *good* photo in the same batch is indexed normally. A single
corrupt byte in a 250,000-photo library must not cost the user the other 249,999.
"""
from __future__ import annotations

import struct
import zipfile
from datetime import datetime
from pathlib import Path

import piexif
import pytest
from PIL import Image

from photointel.pipeline.indexer import Indexer
from tests.conftest import make_image

GOOD = 4          # good photos planted in every hostile library


def index(ctx, roots):
    return Indexer(ctx, workers=2).run(roots=roots)


def plant_good(root: Path) -> set[str]:
    """Known-good photos that must survive whatever else is in the folder."""
    names = set()
    for i in range(GOOD):
        name = f"good_{i}.jpg"
        make_image(root / name, colour=(30 + 40 * i, 90, 160),
                   taken=datetime(2024, 5, 1, 9, i), noise=18)
        names.add(name)
    return names


def indexed_ok(ctx) -> set[str]:
    conn = ctx.connect()
    try:
        return {r[0] for r in conn.execute(
            "SELECT filename FROM photos WHERE status='ok'")}
    finally:
        conn.close()


def statuses(ctx) -> dict[str, str]:
    conn = ctx.connect()
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT filename, status FROM photos")}
    finally:
        conn.close()


# --------------------------------------------------------------- malformed bytes

def test_a_folder_of_broken_files_still_indexes_every_good_photo(ctx, tmp_path):
    """The headline case: eight kinds of broken file, four good photos."""
    root = tmp_path / "junk"
    good = plant_good(root)

    # A JPEG magic number followed by noise - passes a sniff, fails a decode.
    (root / "fake_header.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 400)
    # A PNG that someone renamed to .jpg. Decoders mostly cope; it must not crash.
    make_image(root / "really_a_png.png", fmt="PNG", camera=None)
    (root / "mislabelled.jpg").write_bytes((root / "really_a_png.png").read_bytes())
    (root / "really_a_png.png").unlink()
    # A ZIP wearing a .jpg extension.
    with zipfile.ZipFile(root / "secretly_a_zip.jpg", "w") as z:
        z.writestr("a.txt", "not a photo")
    # A JPEG cut off mid-scan, and one with nothing after the header.
    whole = (root / "good_0.jpg").read_bytes()
    (root / "halved.jpg").write_bytes(whole[: len(whole) // 2])
    (root / "header_only.jpg").write_bytes(whole[:120])
    # Every byte flipped to 0xFF, and a single NUL.
    (root / "all_ff.jpg").write_bytes(b"\xff" * 2048)
    (root / "one_nul.jpg").write_bytes(b"\x00")
    # Valid JPEG, but 1x1 - legal, just useless.
    Image.new("RGB", (1, 1), (10, 20, 30)).save(root / "one_pixel.jpg", quality=90)

    stats = index(ctx, [str(root)])
    assert stats is not None                       # the run completed rather than raising

    missing = good - indexed_ok(ctx)
    assert not missing, f"good photos lost among the broken ones: {sorted(missing)}"

    # Nothing is silently dropped: every file the scanner picked up has a verdict.
    st = statuses(ctx)
    assert all(v in ("ok", "error") for v in st.values()), st


def test_one_corrupt_file_does_not_poison_the_photos_beside_it(ctx, tmp_path):
    """Errors are per file. A broken photo must not mark its batch-mates failed."""
    root = tmp_path / "mixed"
    good = plant_good(root)
    # Interleaved so a bad file lands in the middle of a worker batch.
    for i in range(3):
        (root / f"bad_{i}.jpg").write_bytes(b"\xff\xd8" + bytes([i]) * 300)

    index(ctx, [str(root)])
    st = statuses(ctx)
    for name in sorted(good):
        assert st.get(name) == "ok", f"{name} was collateral damage: {st.get(name)}"
    assert sum(1 for k, v in st.items() if k.startswith("bad_") and v == "error") == 3


def test_a_broken_file_is_retried_not_forgotten(ctx, tmp_path):
    """A file that fails once must be reconsidered when its bytes change: a part
    file that finishes syncing should index on the next run, not stay broken."""
    root = tmp_path / "sync"
    plant_good(root)
    partial = root / "still_syncing.jpg"
    whole = (root / "good_0.jpg").read_bytes()
    partial.write_bytes(whole[:200])

    index(ctx, [str(root)])
    assert statuses(ctx).get("still_syncing.jpg") == "error"

    partial.write_bytes(whole)                     # the sync completes
    index(ctx, [str(root)])
    assert statuses(ctx).get("still_syncing.jpg") == "ok"


# --------------------------------------------------------------- hostile metadata

@pytest.mark.parametrize("stamp", [
    b"0000:00:00 00:00:00",      # the all-zero stamp some cameras write
    b"9999:99:99 99:99:99",      # out of range in every field
    b"2024:13:45 25:70:70",      # plausible shape, impossible values
    b"not a date at all",        # free text
    b"",                         # present but empty
    b"2024:05:01",               # a date with no time
])
def test_impossible_exif_dates_do_not_break_indexing(ctx, tmp_path, stamp):
    root = tmp_path / f"dates_{abs(hash(stamp)) % 10000}"
    good = plant_good(root)
    p = root / "weird_date.jpg"
    make_image(p, taken=datetime(2024, 5, 1, 9, 0))
    ex = piexif.load(str(p))
    ex["Exif"][piexif.ExifIFD.DateTimeOriginal] = stamp
    ex["Exif"][piexif.ExifIFD.DateTimeDigitized] = stamp
    ex["0th"][piexif.ImageIFD.DateTime] = stamp
    piexif.insert(piexif.dump(ex), str(p))

    index(ctx, [str(root)])
    assert good <= indexed_ok(ctx)
    # The photo itself must still index: an unreadable date falls back to the file
    # time, it does not make the photo unusable.
    assert statuses(ctx).get("weird_date.jpg") == "ok", statuses(ctx)
    conn = ctx.connect()
    row = conn.execute(
        "SELECT taken_ts, date_source FROM photos WHERE filename='weird_date.jpg'").fetchone()
    conn.close()
    assert row["taken_ts"] is not None             # something sensible was chosen


@pytest.mark.parametrize("lat,lon", [
    (500.0, 20.0),        # beyond the poles
    (20.0, 400.0),        # beyond the meridian
    (0.0, 0.0),           # null island: valid, but it almost always means "no fix"
])
def test_out_of_range_gps_does_not_break_indexing(ctx, tmp_path, lat, lon):
    root = tmp_path / f"gps_{abs(hash((lat, lon))) % 10000}"
    good = plant_good(root)
    p = root / "bad_gps.jpg"
    try:
        make_image(p, taken=datetime(2024, 5, 1, 9, 0), gps=(lat, lon))
    except (ValueError, OverflowError, struct.error):
        pytest.skip("piexif refuses to write this coordinate, so no camera could produce it")

    index(ctx, [str(root)])
    assert good <= indexed_ok(ctx)
    assert statuses(ctx).get("bad_gps.jpg") in ("ok", "error")
    conn = ctx.connect()
    row = conn.execute(
        "SELECT gps_lat, gps_lon FROM photos WHERE filename='bad_gps.jpg'").fetchone()
    conn.close()
    if row and row["gps_lat"] is not None:
        # If a coordinate is stored at all it has to be on the planet, or every map
        # view and every reverse-geocode downstream inherits the nonsense.
        assert -90 <= row["gps_lat"] <= 90, row["gps_lat"]
        assert -180 <= row["gps_lon"] <= 180, row["gps_lon"]


def test_garbage_exif_block_does_not_break_indexing(ctx, tmp_path):
    """An EXIF segment full of noise - a real failure mode of damaged SD cards."""
    root = tmp_path / "badexif"
    good = plant_good(root)
    p = root / "noisy_exif.jpg"
    make_image(p, taken=datetime(2024, 5, 1, 9, 0))
    raw = bytearray(p.read_bytes())
    start = raw.find(b"Exif\x00\x00")
    assert start > 0, "the fixture no longer writes an EXIF segment to scribble on"
    for i in range(start + 6, min(start + 200, len(raw))):
        raw[i] = (i * 37) % 256
    p.write_bytes(bytes(raw))

    index(ctx, [str(root)])
    assert good <= indexed_ok(ctx)
    assert statuses(ctx).get("noisy_exif.jpg") in ("ok", "error")


@pytest.mark.parametrize("orientation", [0, 9, 255])
def test_invalid_exif_orientation_is_survivable(ctx, tmp_path, orientation):
    root = tmp_path / f"orient_{orientation}"
    good = plant_good(root)
    p = root / "spun.jpg"
    try:
        make_image(p, taken=datetime(2024, 5, 1, 9, 0), orientation=orientation)
    except (ValueError, OverflowError, struct.error):
        pytest.skip("piexif refuses to write this orientation")

    index(ctx, [str(root)])
    assert good <= indexed_ok(ctx)
    assert statuses(ctx).get("spun.jpg") == "ok"


# --------------------------------------------------------------- hostile names

@pytest.mark.parametrize("name", [
    "famille_ete_2024.jpg",
    "照片_2024.jpg",                    # CJK
    "صورة.jpg",                        # RTL
    "photo party.jpg",
    "ПРИВЕТ.jpg",                      # Cyrillic
    "double..dots.jpg",
    "'quoted'.jpg",
    "semi;colon&amp.jpg",
    "100% sure.jpg",                   # percent, which breaks naive URL handling
    # A long name. 100 rather than the 255 a filesystem allows: the whole path has
    # to stay under Windows' 260-character limit, and pytest's tmp dir is already long.
    "a" * 100 + ".jpg",
])
def test_awkward_filenames_index_and_round_trip(ctx, tmp_path, name):
    """Unicode and punctuation in names must survive the scanner and the database
    without being mangled."""
    root = tmp_path / "names"
    root.mkdir(parents=True, exist_ok=True)
    try:
        make_image(root / name, taken=datetime(2024, 5, 1, 9, 0), noise=12)
    except (OSError, UnicodeEncodeError):
        pytest.skip(f"this filesystem will not store a file named {name!r}")

    index(ctx, [str(root)])
    conn = ctx.connect()
    row = conn.execute("SELECT id, filename, rel_path FROM photos").fetchone()
    conn.close()
    assert row is not None, f"{name!r} was never indexed"
    assert row["filename"] == name, f"name mangled: {row['filename']!r} != {name!r}"


def test_an_emoji_filename_survives(ctx, tmp_path):
    """Separate from the parametrised names: emoji are outside the BMP and are the
    case most likely to be mangled by a narrow encoding somewhere in the stack."""
    root = tmp_path / "emoji"
    root.mkdir(parents=True, exist_ok=True)
    name = "party \U0001F389 time.jpg"
    try:
        make_image(root / name, taken=datetime(2024, 5, 1, 9, 0), noise=12)
    except (OSError, UnicodeEncodeError):
        pytest.skip("the filesystem will not store an emoji name")

    index(ctx, [str(root)])
    conn = ctx.connect()
    row = conn.execute("SELECT filename FROM photos").fetchone()
    conn.close()
    assert row is not None and row["filename"] == name


def test_deeply_nested_folders_are_scanned(ctx, tmp_path):
    """Some backup tools produce deep nesting must not stop the walk (12 levels, which is all
    that fits under the Windows 260-character path limit from a pytest tmp dir)."""
    root = tmp_path / "deep"
    nested = root.joinpath(*[f"level{i}" for i in range(12)])
    make_image(nested / "buried.jpg", taken=datetime(2024, 5, 1, 9, 0))
    make_image(root / "shallow.jpg", taken=datetime(2024, 5, 1, 9, 1), colour=(200, 60, 60))

    index(ctx, [str(root)])
    assert {"buried.jpg", "shallow.jpg"} <= indexed_ok(ctx)


# --------------------------------------------------------------- hostile dimensions

def test_extreme_aspect_ratios_index_with_real_thumbnails(ctx, tmp_path):
    """Panoramas and long screenshots: thumbnailing must not divide by zero or
    produce a zero-pixel thumbnail."""
    root = tmp_path / "shapes"
    good = plant_good(root)
    make_image(root / "panorama.jpg", size=(12000, 240), taken=datetime(2024, 5, 1, 9, 0))
    make_image(root / "tall_screenshot.jpg", size=(240, 12000), taken=datetime(2024, 5, 1, 9, 1))
    make_image(root / "hairline.jpg", size=(4000, 1), taken=datetime(2024, 5, 1, 9, 2))

    index(ctx, [str(root)])
    ok = indexed_ok(ctx)
    assert good <= ok
    for n in ("panorama.jpg", "tall_screenshot.jpg", "hairline.jpg"):
        assert n in ok, f"{n} failed to index"

    # Thumbnails are content-addressed, so check the lot: every one must be a real
    # image. A 4000x1 source rounded to a zero-height thumbnail would be invisible
    # in the grid rather than obviously broken.
    thumbs = list(ctx.paths.thumbs.rglob("*.webp"))
    assert thumbs, "no thumbnails were written at all"
    for tp in thumbs:
        assert tp.stat().st_size > 0, f"empty thumbnail {tp.name}"
        with Image.open(tp) as im:
            assert im.width > 0 and im.height > 0, f"0-pixel thumbnail {tp.name}"


def test_a_decompression_bomb_is_refused_not_fatal(ctx, tmp_path):
    """A small file that declares an enormous canvas. Pillow raises rather than
    allocating; the indexer must record that and carry on."""
    root = tmp_path / "bomb"
    good = plant_good(root)
    try:
        # 46,000 x 46,000 is 2.1 gigapixels but compresses to a few KB.
        big = Image.new("L", (46000, 46000))
        big.save(root / "bomb.png", optimize=True)
        del big
    except (MemoryError, OSError, ValueError):
        pytest.skip("cannot build a bomb on this machine")

    index(ctx, [str(root)])
    assert good <= indexed_ok(ctx), "a decompression bomb cost us the good photos"
    assert statuses(ctx).get("bomb.png") in ("ok", "error")
