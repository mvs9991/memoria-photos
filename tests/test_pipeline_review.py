"""Indexing bugs found by a review (2026-10-05). Each failed before its fix."""
import os, subprocess, sys, time
from datetime import datetime
from pathlib import Path

import piexif
from PIL import Image

from photointel import metadata
from photointel.engine import trash
from photointel.pipeline import scanner
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from tests.conftest import make_image


def index(ctx, *roots):
    return Indexer(ctx, workers=2).run(roots=[str(r) for r in roots])


def test_a_root_inside_another_root_is_indexed_once(ctx, tmp_path):
    outer = tmp_path / "Photos"
    make_image(outer / "2020" / "a.jpg", taken=datetime(2020, 1, 1, 10), noise=10)
    index(ctx, outer, outer / "2020")
    conn = ctx.connect()
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE status = 'ok'").fetchone()[0] == 1


def test_a_utc_offset_of_zero_is_recorded(ctx, tmp_path):
    p = tmp_path / "lib" / "london.jpg"
    p.parent.mkdir(parents=True)
    exif = {"0th": {piexif.ImageIFD.Make: b"Apple"}, "Exif": {
        piexif.ExifIFD.DateTimeOriginal: b"2024:01:10 12:00:00", 0x9011: b"+00:00"},
        "GPS": {}, "1st": {}, "thumbnail": None}
    Image.new("RGB", (64, 64), (10, 20, 30)).save(p, "JPEG", exif=piexif.dump(exif))
    index(ctx, p.parent)
    assert ctx.connect().execute("SELECT tz_offset_min FROM photos").fetchone()[0] == 0
    assert metadata.parse_offset("+99:99") is None          # not a real offset


def test_an_epoch_filename_is_dated_in_local_time_like_every_other_source():
    dt, _, _ = metadata.date_from_filename("FB_IMG_1565524312345.jpg")
    assert dt.replace(microsecond=0) == datetime.fromtimestamp(1565524312)


def test_a_file_dated_before_1970_still_indexes(ctx, tmp_path):
    p = tmp_path / "lib" / "old.png"
    p.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), (10, 20, 30)).save(p, "PNG")
    os.utime(p, (-86400 * 400, -86400 * 400))
    index(ctx, p.parent)
    assert ctx.connect().execute("SELECT status FROM photos").fetchone()[0] == "ok"


def _geo(ctx):
    rows = [["1269843", "Hyderabad", "Hyderabad", "", "17.38405", "78.45636", "P", "PPLA", "IN", "", "40", "", "", "",
             "3597816", "", "536", "Asia/Kolkata", "2020-01-01"],
            ["1259229", "Panaji", "Panaji", "", "15.4909", "73.8278", "P", "PPLA", "IN", "", "33", "", "", "",
             "114405", "", "10", "Asia/Kolkata", "2020-01-01"]]
    (ctx.paths.geo / "cities500.txt").write_text("\n".join("\t".join(r) for r in rows) + "\n", encoding="utf-8")
    (ctx.paths.geo / "admin1CodesASCII.txt").write_text("IN.40\tTelangana\tTelangana\t1\nIN.33\tGoa\tGoa\t1\n", encoding="utf-8")
    (ctx.paths.geo / "countryInfo.txt").write_text("IN\tIND\t356\tIN\tIndia\n", encoding="utf-8")
    from photointel.geo import ReverseGeocoder
    ReverseGeocoder._instance = None


def test_a_file_whose_gps_changed_is_geocoded_again(ctx, tmp_path):
    _geo(ctx)
    root = tmp_path / "lib"
    make_image(root / "a.jpg", taken=datetime(2024, 1, 1, 10), gps=(17.385, 78.4867))
    index(ctx, root)
    run_post_stages(ctx, ctx.connect(), stages=["geocode"])
    time.sleep(1.1)
    make_image(root / "a.jpg", taken=datetime(2024, 1, 1, 10), gps=(15.49, 73.83), colour=(1, 2, 3))
    index(ctx, root)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["geocode"])
    city = conn.execute("SELECT pl.city FROM photos p JOIN places pl ON pl.id = p.place_id").fetchone()[0]
    from photointel.geo import ReverseGeocoder
    ReverseGeocoder._instance = None
    assert city == "Panaji"


def test_a_folder_that_cannot_be_read_does_not_mark_its_photos_missing(ctx, tmp_path, monkeypatch):
    root = tmp_path / "lib"
    make_image(root / "A" / "a.jpg", taken=datetime(2024, 1, 1, 10), noise=5)
    make_image(root / "B" / "b.jpg", taken=datetime(2024, 1, 2, 10), noise=6)
    index(ctx, root)
    real = os.scandir

    def flaky(path="."):
        if str(path).endswith(os.sep + "B"):
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(scanner.os, "scandir", flaky)
    index(ctx, root)
    assert dict(ctx.connect().execute("SELECT filename, status FROM photos").fetchall())["b.jpg"] == "ok"


def test_photos_are_not_hidden_for_good_when_the_drive_drops_during_analysis(ctx, tmp_path):
    root = tmp_path / "drive" / "Photos"
    for i in range(3):
        make_image(root / f"p{i}.jpg", taken=datetime(2024, 1, 1, 10, i), noise=5 + i)
    index(ctx, root)
    conn = ctx.connect()
    conn.execute("UPDATE photos SET semantic_model = NULL")       # e.g. a model upgrade: re-embed everything
    conn.commit()
    ix = Indexer(ctx, workers=2)
    ix.scan([str(root)])
    os.rename(tmp_path / "drive", tmp_path / "unplugged")           # the drive goes away mid-run
    ix.run(roots=None)
    os.rename(tmp_path / "unplugged", tmp_path / "drive")           # and comes back
    index(ctx, root)                                                # the next scheduled run
    assert {r[0] for r in ctx.connect().execute("SELECT status FROM photos")} == {"ok"}


REPO = str(Path(__file__).resolve().parents[1])
LOCKER = r'''
import sys, time
sys.path.insert(0, sys.argv[3])
from pathlib import Path
from photointel.pipeline.jobs import IndexLock
start = float(sys.argv[2])
while time.time() < start:
    pass
print(IndexLock(Path(sys.argv[1]) / "index.lock").acquire(), flush=True)
time.sleep(2)
'''


def test_two_processes_cannot_both_take_the_index_lock(tmp_path):
    script = tmp_path / "locker.py"
    script.write_text(LOCKER, encoding="utf-8")
    for trial in range(5):
        d = tmp_path / f"data{trial}"
        d.mkdir()
        start = time.time() + 3
        procs = [subprocess.Popen([sys.executable, str(script), str(d), str(start), REPO], stdout=subprocess.PIPE, text=True)
                 for _ in range(2)]
        got = [p.communicate()[0].strip() for p in procs]
        assert got.count("True") == 1, got


def test_a_new_file_at_a_trashed_photos_path_does_not_take_its_place(ctx, tmp_path):
    root = tmp_path / "lib"
    make_image(root / "IMG_0001.jpg", taken=datetime(2023, 5, 1, 10), noise=5)
    index(ctx, root)
    conn = ctx.connect()
    pid = conn.execute("SELECT id FROM photos").fetchone()[0]
    conn.execute("UPDATE photos SET rating = 5 WHERE id = ?", (pid,))
    conn.commit()
    trash.move_to_trash(ctx, conn, [pid])
    make_image(root / "IMG_0001.jpg", taken=datetime(2025, 2, 2, 9), colour=(200, 30, 30), noise=9)
    index(ctx, root)
    conn = ctx.connect()
    assert conn.execute("SELECT status FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "trashed"
    new = conn.execute("SELECT rating FROM photos WHERE id != ? AND status = 'ok'", (pid,)).fetchone()
    assert new is not None and not new[0]


LONG = chr(92) * 2 + "?" + chr(92)


def test_photos_under_a_long_path_are_indexed(ctx, tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    deep = root
    for part in ("x" * 100, "y" * 100, "z" * 100):
        deep = deep / part
        os.mkdir(LONG + str(deep))
    src = make_image(tmp_path / "src.jpg", taken=datetime(2024, 1, 1, 10), noise=5)
    with open(src, "rb") as f, open(LONG + str(deep / "long.jpg"), "wb") as g:
        g.write(f.read())
    index(ctx, root)
    assert [r[0] for r in ctx.connect().execute("SELECT status FROM photos")] == ["ok"]
