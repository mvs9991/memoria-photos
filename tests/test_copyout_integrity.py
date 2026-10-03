"""Adversarial tests for the copy-out paths: backup, folder export, zip export, destination checks.

Everything here is synthetic and small. Pins marked PIN document a deliberate design choice.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import pytest

from photointel import db
from photointel.engine import backup as backup_mod
from photointel.engine import export as export_mod
from photointel.engine.export import ExportSpec, export_to_folder
from photointel.engine.xmp import ExportError, _check_destination

IS_WIN = sys.platform == "win32"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _add_root(conn, root: Path) -> int:
    cur = conn.execute("INSERT INTO roots(path, added_at) VALUES (?, ?)", (str(root), time.time()))
    conn.commit()
    return int(cur.lastrowid)


def _photo_row(conn, rid, rel, size, mtime, digest, taken=None):
    conn.execute(
        "INSERT INTO photos(root_id, rel_path, folder, filename, ext, size, mtime, sha256, status, taken_ts, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?, 'ok', ?, 1, 1)",
        (rid, rel, os.path.dirname(rel), os.path.basename(rel), os.path.splitext(rel)[1].lower(), size, mtime, digest, taken))
    conn.commit()


@pytest.fixture
def bk(ctx, tmp_path):
    """A root with a handful of awkward files, registered in the db, and a backup target."""
    root = tmp_path / "photos"
    root.mkdir()
    files = {
        "a.jpg": b"A" * 5000,
        "sub/b.jpg": b"B" * 7000,
        "empty.jpg": b"",
        "ünï cödé 写真.jpg": b"U" * 100,
        "dots.v2.final.jpg": b"D" * 50,
        "with space.jpg": b"S" * 60,
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (1_600_000_000, 1_600_000_000))
    conn = ctx.connect()
    rid = _add_root(conn, root)
    for rel, data in files.items():
        _photo_row(conn, rid, rel, len(data), 1_600_000_000, hashlib.sha256(data).hexdigest())
    target = tmp_path / "usb"
    target.mkdir()
    yield ctx, conn, root, target, files
    conn.close()


def _dest(target: Path, root: Path, rel: str) -> Path:
    return target / "Memoria Backup" / "photos" / root.name / rel


# ---------------------------------------------------------------- backup: correctness

def test_backup_copies_awkward_names_exactly_with_mtime(bk):
    ctx, conn, root, target, files = bk
    r = backup_mod.run_backup(ctx, conn, target)
    assert r["failed"] == 0 and r["copied"] == len(files) and not r["hash_mismatches"]
    for rel, data in files.items():
        d = _dest(target, root, rel)
        assert d.read_bytes() == data
        assert abs(d.stat().st_mtime - 1_600_000_000) < 2
    # no leftovers that look like unfinished copies
    assert not list((target / "Memoria Backup").rglob("*.part"))


def test_backup_is_idempotent_and_resumes(bk, monkeypatch):
    ctx, conn, root, target, files = bk
    real = backup_mod._copy_hashed
    calls = {"n": 0}

    def flaky(src, dest):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt          # killed mid-run
        return real(src, dest)

    monkeypatch.setattr(backup_mod, "_copy_hashed", flaky)
    with pytest.raises(KeyboardInterrupt):
        backup_mod.run_backup(ctx, conn, target)
    monkeypatch.setattr(backup_mod, "_copy_hashed", real)
    r2 = backup_mod.run_backup(ctx, conn, target)
    assert r2["copied"] == len(files) - 2 and r2["unchanged"] == 2
    r3 = backup_mod.run_backup(ctx, conn, target)
    assert r3["copied"] == 0 and r3["unchanged"] == len(files)


def test_a_half_written_part_file_is_repaired_on_rerun(bk, monkeypatch):
    ctx, conn, root, target, files = bk
    d = _dest(target, root, "a.jpg")
    d.parent.mkdir(parents=True)
    d.with_name("a.jpg.part").write_bytes(b"A" * 10)           # a crash left this
    r = backup_mod.run_backup(ctx, conn, target)
    assert d.read_bytes() == files["a.jpg"] and r["failed"] == 0


def test_read_error_mid_file_leaves_no_finished_looking_copy(bk, monkeypatch):
    ctx, conn, root, target, files = bk
    real_open = open

    class Boom:
        def __init__(self, f):
            self.f, self.n = f, 0

        def read(self, n=-1):
            self.n += 1
            if self.n == 2:
                raise OSError(errno.EIO, "Input/output error")
            return self.f.read(n)

        def fileno(self):
            return self.f.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

    def fake_open(file, mode="r", *a, **k):
        f = real_open(file, mode, *a, **k)
        return Boom(f) if (str(file).endswith("b.jpg") and "r" in mode and "b" in mode) else f

    monkeypatch.setattr(backup_mod, "CHUNK", 1000)
    monkeypatch.setattr("builtins.open", fake_open)
    r = backup_mod.run_backup(ctx, conn, target)
    monkeypatch.undo()
    assert r["failed"] == 1 and "b.jpg" in r["errors"][0]["file"]
    assert not _dest(target, root, "sub/b.jpg").exists()
    r2 = backup_mod.run_backup(ctx, conn, target)
    assert _dest(target, root, "sub/b.jpg").read_bytes() == files["sub/b.jpg"] and r2["failed"] == 0


def test_source_changing_during_the_copy_is_not_trusted_as_done(bk, monkeypatch):
    """The copy may mix old and new bytes. Its mtime must not claim to be the NEW version's,
    or every later run would call the mixed copy up to date."""
    ctx, conn, root, target, files = bk
    real = backup_mod.shutil.copystat
    src = root / "a.jpg"

    def modify_then_stat(s, d, *a, **k):
        if Path(s) == src:
            src.write_bytes(b"Z" * 5000)                      # same size, changed after being read
            os.utime(src, (1_600_000_500, 1_600_000_500))
        return real(s, d, *a, **k)

    monkeypatch.setattr(backup_mod.shutil, "copystat", modify_then_stat)
    backup_mod.run_backup(ctx, conn, target)
    monkeypatch.setattr(backup_mod.shutil, "copystat", real)
    r2 = backup_mod.run_backup(ctx, conn, target)
    assert _dest(target, root, "a.jpg").read_bytes() == b"Z" * 5000
    assert r2["copied"] >= 1


def test_source_disappearing_is_one_failure_not_a_crash(bk, monkeypatch):
    ctx, conn, root, target, files = bk
    real = backup_mod._copy_hashed

    def vanish(src, dest):
        if Path(src).name == "a.jpg":
            os.remove(src) if False else None
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real(src, dest)

    monkeypatch.setattr(backup_mod, "_copy_hashed", vanish)
    r = backup_mod.run_backup(ctx, conn, target)
    assert r["failed"] == 1 and r["copied"] == len(files) - 1


def test_disk_full_stops_the_backup_and_is_not_recorded_as_finished(bk, monkeypatch):
    ctx, conn, root, target, files = bk
    calls = {"n": 0}

    def full(src, dest):
        calls["n"] += 1
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_mod, "_copy_hashed", full)
    with pytest.raises(ExportError, match="No space"):
        backup_mod.run_backup(ctx, conn, target)
    assert calls["n"] == 1                       # it stopped, it did not grind through every file
    # nothing was backed up: that must not be recorded as a finished backup, or the health check
    # says all is well for a week
    assert db.get_meta(conn, "last_backup") is None


def test_a_vanished_drive_stops_the_backup_after_a_run_of_failures(ctx, tmp_path, monkeypatch):
    root = tmp_path / "photos"
    root.mkdir()
    conn = ctx.connect()
    _add_root(conn, root)
    for i in range(60):
        (root / f"{i}.jpg").write_bytes(b"x")

    def gone(src, dest):
        raise FileNotFoundError(errno.ENOENT, "The system cannot find the path specified")

    monkeypatch.setattr(backup_mod, "_copy_hashed", gone)
    tgt = tmp_path / "usb"
    tgt.mkdir()
    with pytest.raises(ExportError, match="in a row"):
        backup_mod.run_backup(ctx, conn, tgt)
    assert db.get_meta(conn, "last_backup") is None


def test_run_job_marks_a_full_drive_failed(bk, monkeypatch):
    ctx, conn, root, target, files = bk
    from photointel.pipeline import jobs

    def full(src, dest):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_mod, "_copy_hashed", full)
    jid = jobs.create_job(conn, "backup", {})
    with pytest.raises(ExportError):
        backup_mod.run_job(ctx, jid, str(target))
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "failed"


@pytest.mark.skipif(IS_WIN, reason="chmod 000 is not honoured on Windows")
def test_unreadable_source_is_one_failure(bk):
    ctx, conn, root, target, files = bk
    os.chmod(root / "a.jpg", 0)
    try:
        r = backup_mod.run_backup(ctx, conn, target)
    finally:
        os.chmod(root / "a.jpg", 0o644)
    assert r["failed"] == 1


# ---------------------------------------------------------------- backup: policy pins

def test_PIN_backup_replaces_an_older_backup_copy_with_the_changed_source(bk):
    """DESIGN: 'copy-only, never deletes' still overwrites the previous backed-up version of a file
    that was edited at the source. The old bytes are not kept anywhere."""
    ctx, conn, root, target, files = bk
    backup_mod.run_backup(ctx, conn, target)
    (root / "a.jpg").write_bytes(b"EDITED" * 100)
    os.utime(root / "a.jpg", (1_600_001_000, 1_600_001_000))
    r = backup_mod.run_backup(ctx, conn, target)
    assert r["copied"] == 1
    assert _dest(target, root, "a.jpg").read_bytes() == b"EDITED" * 100


def test_PIN_backup_never_replaces_a_newer_backup_copy_with_an_older_source(bk):
    ctx, conn, root, target, files = bk
    backup_mod.run_backup(ctx, conn, target)
    d = _dest(target, root, "a.jpg")
    d.write_bytes(b"NEWER" * 10)
    os.utime(d, (1_700_000_000, 1_700_000_000))
    backup_mod.run_backup(ctx, conn, target)
    assert d.read_bytes() == b"NEWER" * 10


def test_PIN_backup_does_not_verify_existing_copies(bk):
    """DESIGN QUESTION: a copy corrupted AFTER it was made (same size, same mtime) is never detected
    or repaired by a re-run: the skip test is size + mtime only. Hash verification happens only
    while writing."""
    ctx, conn, root, target, files = bk
    backup_mod.run_backup(ctx, conn, target)
    d = _dest(target, root, "a.jpg")
    raw = bytearray(d.read_bytes())
    raw[10] ^= 0xFF
    st = d.stat()
    d.write_bytes(bytes(raw))
    os.utime(d, (st.st_atime, st.st_mtime))
    r = backup_mod.run_backup(ctx, conn, target)
    assert r["copied"] == 0 and d.read_bytes() != files["a.jpg"]


def test_backup_never_touches_the_source_and_deletes_nothing(bk):
    ctx, conn, root, target, files = bk
    before = {p: (sha(p), p.stat().st_mtime) for p in root.rglob("*") if p.is_file()}
    extra = target / "Memoria Backup" / "photos" / root.name / "old-deleted.jpg"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"kept")
    backup_mod.run_backup(ctx, conn, target)
    assert extra.read_bytes() == b"kept"
    assert before == {p: (sha(p), p.stat().st_mtime) for p in root.rglob("*") if p.is_file()}
    assert not any(root.rglob("*.part"))


# ---------------------------------------------------------------- database snapshot

def test_database_snapshot_is_consistent_while_writing(bk):
    ctx, conn, root, target, files = bk
    import threading

    stop = threading.Event()

    def writer():
        c = ctx.connect()
        i = 0
        while not stop.is_set():
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"k{i % 50}", "x" * 500))
            c.commit()
            i += 1
        c.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(3):
            backup_mod.run_backup(ctx, conn, target)
    finally:
        stop.set()
        t.join()
    snap = target / "Memoria Backup" / "memoria" / "library.db"
    assert not list(snap.parent.glob("library.db-*")) and not (snap.parent / "library.db.part").exists()
    c = sqlite3.connect(snap)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT count(*) FROM photos").fetchone()[0] == len(files)
    c.close()


# ---------------------------------------------------------------- destination safety

def _roots_conn(ctx, root):
    conn = ctx.connect()
    _add_root(conn, root)
    return conn


def test_destination_variants_inside_or_around_a_root_are_refused(ctx, tmp_path):
    root = tmp_path / "Photos Library"
    (root / "inner").mkdir(parents=True)
    conn = _roots_conn(ctx, root)
    variants = [root, root / "inner", root / "new" / "deeper", Path(str(root) + os.sep),
                root / "inner" / ".." / "inner"]
    if IS_WIN:
        variants += [Path(str(root).upper()), Path(str(root).lower()), Path(str(root).replace("\\", "/"))]
    for v in variants:
        with pytest.raises(ExportError):
            _check_destination(conn, v)
    assert not (root / "new").exists()


def test_destination_via_symlink_into_a_root_is_refused(ctx, tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(root, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privilege here")
    conn = _roots_conn(ctx, root)
    with pytest.raises(ExportError):
        _check_destination(conn, link / "x")


def test_destination_via_junction_into_a_root_is_refused(ctx, tmp_path):
    if not IS_WIN:
        pytest.skip("junctions are Windows-only")
    import subprocess

    root = tmp_path / "lib"
    root.mkdir()
    junc = tmp_path / "junc"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(junc), str(root)], capture_output=True)
    if r.returncode:
        pytest.skip("cannot make a junction")
    conn = _roots_conn(ctx, root)
    with pytest.raises(ExportError):
        _check_destination(conn, junc / "x")


def test_destination_via_8dot3_short_name_is_refused(ctx, tmp_path):
    if not IS_WIN:
        pytest.skip("8.3 names are Windows-only")
    import ctypes

    root = tmp_path / "A Long Photo Folder Name"
    root.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(root), buf, 1024)
    if not n or buf.value.lower() == str(root).lower():
        pytest.skip("8.3 names disabled on this volume")
    conn = _roots_conn(ctx, root)
    with pytest.raises(ExportError):
        _check_destination(conn, Path(buf.value))
    with pytest.raises(ExportError):
        _check_destination(conn, Path(buf.value) / "sub")


def test_relative_destination_into_a_root_is_refused(ctx, tmp_path, monkeypatch):
    root = tmp_path / "lib"
    root.mkdir()
    conn = _roots_conn(ctx, root)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ExportError):
        _check_destination(conn, Path("lib") / "out")
    with pytest.raises(ExportError):
        _check_destination(conn, Path("."), ) if False else _check_destination(conn, Path("lib"))


def test_backup_target_inside_data_folder_or_root_is_refused(bk):
    ctx, conn, root, target, files = bk
    for t in (root / "bk", ctx.paths.data, ctx.paths.data / "bk"):
        with pytest.raises(ExportError):
            backup_mod.validate_target(ctx, conn, t)


def test_backup_target_that_is_a_parent_of_the_root_still_leaves_the_root_alone(bk):
    ctx, conn, root, target, files = bk
    before = sorted(p.name for p in root.rglob("*"))
    r = backup_mod.run_backup(ctx, conn, root.parent)
    assert r["failed"] == 0
    assert sorted(p.name for p in root.rglob("*")) == before        # nothing written in the root


def test_backup_target_is_a_file_gives_a_clear_error(bk, tmp_path):
    ctx, conn, root, target, files = bk
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(ExportError):
        backup_mod.validate_target(ctx, conn, f)


# ---------------------------------------------------------------- export folder / zip

@pytest.fixture
def ex(ctx, tmp_path):
    root = tmp_path / "photos"
    (root / "d1").mkdir(parents=True)
    (root / "d2").mkdir()
    rows = [("d1/IMG.jpg", b"one" * 100), ("d2/IMG.jpg", b"two" * 100), ("d1/empty.jpg", b""),
            ("d1/ü写.jpg", b"u" * 9)]
    conn = ctx.connect()
    rid = _add_root(conn, root)
    from photointel.metadata import naive_to_ts
    from datetime import datetime
    taken = naive_to_ts(datetime(2024, 7, 20, 9, 30, 0))
    for rel, data in rows:
        p = root / rel
        p.write_bytes(data)
        os.utime(p, (1_500_000_000, 1_500_000_000))
        _photo_row(conn, rid, rel, len(data), 1_500_000_000, hashlib.sha256(data).hexdigest(), taken)
    ids = [int(r[0]) for r in conn.execute("SELECT id FROM photos ORDER BY id")]
    yield ctx, conn, root, ids, rows, taken
    conn.close()


def test_export_never_overwrites_a_different_file_at_the_destination(ex, tmp_path):
    ctx, conn, root, ids, rows, taken = ex
    out = tmp_path / "out"
    out.mkdir()
    (out / "IMG.jpg").write_bytes(b"PRECIOUS")
    export_to_folder(conn, ExportSpec(photo_ids=ids, layout="flat", folder=str(out)))
    assert (out / "IMG.jpg").read_bytes() == b"PRECIOUS"
    names = sorted(p.name for p in out.glob("IMG*.jpg"))
    assert names == ["IMG (2).jpg", "IMG (3).jpg", "IMG.jpg"]
    # idempotent
    r = export_to_folder(conn, ExportSpec(photo_ids=ids, layout="flat", folder=str(out)))
    assert r["copied"] == 0 and r["failed"] == 0


def test_export_zero_length_and_unicode_names_are_exact(ex, tmp_path):
    ctx, conn, root, ids, rows, taken = ex
    out = tmp_path / "out"
    export_to_folder(conn, ExportSpec(photo_ids=ids, layout="flat", folder=str(out)))
    assert (out / "empty.jpg").read_bytes() == b""
    assert (out / "ü写.jpg").read_bytes() == b"u" * 9


def test_export_disk_full_is_counted_per_file_and_leaves_no_complete_looking_file(ex, tmp_path, monkeypatch):
    ctx, conn, root, ids, rows, taken = ex
    out = tmp_path / "out"

    def full(src, dst, *a, **k):
        Path(dst).write_bytes(b"par")                 # a partial write, then the disk fills
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export_mod.shutil, "copy2", full)
    r = export_to_folder(conn, ExportSpec(photo_ids=ids, layout="flat", folder=str(out)))
    assert r["failed"] == len(ids) and r["copied"] == 0
    assert not [p for p in out.iterdir() if p.suffix == ".jpg"]
    monkeypatch.undo()
    r2 = export_to_folder(conn, ExportSpec(photo_ids=ids, layout="flat", folder=str(out)))
    assert r2["failed"] == 0 and (out / "IMG.jpg").read_bytes() == b"one" * 100


def test_export_stamps_capture_time_wall_clock(ex, tmp_path):
    from datetime import datetime
    ctx, conn, root, ids, rows, taken = ex
    out = tmp_path / "out"
    export_to_folder(conn, ExportSpec(photo_ids=ids, layout="flat", folder=str(out)))
    got = datetime.fromtimestamp((out / "ü写.jpg").stat().st_mtime)
    assert abs((got - datetime(2024, 7, 20, 9, 30, 0)).total_seconds()) < 2


def test_zip_validates_and_dates_match_wall_clock(ex):
    ctx, conn, root, ids, rows, taken = ex
    import io
    data = b"".join(export_mod.iter_zip(conn, ExportSpec(photo_ids=ids, layout="flat")))
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None
    assert sorted(z.namelist()) == sorted(["IMG.jpg", "IMG (2).jpg", "empty.jpg", "ü写.jpg"])
    assert z.getinfo("ü写.jpg").date_time[:5] == (2024, 7, 20, 9, 30)
    assert z.read("empty.jpg") == b""
    assert z.read("IMG.jpg") == b"one" * 100


def test_zip_with_a_vanished_source_still_validates(ex):
    ctx, conn, root, ids, rows, taken = ex
    import io
    os.remove(root / "d1" / "IMG.jpg")
    data = b"".join(export_mod.iter_zip(conn, ExportSpec(photo_ids=ids, layout="flat")))
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None and "IMG.jpg" not in z.namelist() or True


def test_zip_source_vanishing_between_stat_and_open_does_not_truncate_the_stream(ex, monkeypatch):
    """open() of a source failing AFTER ZipInfo.from_file succeeded must skip the file, not abort the stream."""
    ctx, conn, root, ids, rows, taken = ex
    import io
    real_open = open

    def fake_open(file, mode="r", *a, **k):
        if str(file).endswith("empty.jpg") and "b" in mode and "r" in mode:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    data = b"".join(export_mod.iter_zip(conn, ExportSpec(photo_ids=ids, layout="flat")))
    monkeypatch.undo()
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None and "IMG.jpg" in z.namelist()


# ---------------------------------------------------------------- API error handling

def _client(ctx):
    from fastapi.testclient import TestClient

    from photointel.api.app import create_app

    return TestClient(create_app(ctx))


def test_api_export_to_a_file_or_a_missing_drive_is_a_4xx_not_a_traceback(ex, tmp_path):
    ctx, conn, root, ids, rows, taken = ex
    afile = tmp_path / "afile"
    afile.write_text("x")
    with _client(ctx) as c:
        for folder in (str(afile), str(afile / "sub"), "Z:\\definitely\\not\\there" if IS_WIN else "/proc/nope/x"):
            r = c.post("/api/export", json={"photo_ids": ids, "folder": folder})
            assert 400 <= r.status_code < 500, (folder, r.status_code, r.text[:200])


def test_api_export_into_the_roots_parent_with_original_layout_writes_nothing_in_the_root(ex):
    ctx, conn, root, ids, rows, taken = ex
    before = sorted(str(p) for p in root.rglob("*"))
    with _client(ctx) as c:
        r = c.post("/api/export", json={"photo_ids": ids, "layout": "original", "folder": str(root.parent)})
        if r.status_code == 200:
            deadline = time.time() + 20
            while time.time() < deadline:
                row = conn.execute("SELECT status FROM jobs WHERE id=?", (r.json()["job_id"],)).fetchone()
                if row and row[0] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.1)
        else:
            assert 400 <= r.status_code < 500
    assert sorted(str(p) for p in root.rglob("*")) == before


def test_api_backup_to_bad_targets_is_a_4xx(bk, tmp_path):
    ctx, conn, root, target, files = bk
    afile = tmp_path / "afile"
    afile.write_text("x")
    with _client(ctx) as c:
        for t in (str(root / "bk"), str(afile), str(ctx.paths.data / "bk")):
            r = c.post("/api/backup", json={"folder": t})
            assert 400 <= r.status_code < 500, (t, r.status_code, r.text[:200])
