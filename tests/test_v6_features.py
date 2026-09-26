"""v6: rotation, automatic indexing, uploads, backup, iCloud import, archive, locked folder,
accounts, contributions to shared albums, editing as copies, creations, colours."""
from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photointel.pipeline.indexer import Indexer
from photointel.rotation import rotate_box


def index(ctx, root) -> None:
    Indexer(ctx, workers=2).run(roots=[str(root)])


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def client(ctx):
    from photointel.api.app import create_app

    with TestClient(create_app(ctx)) as c:
        yield c


def pid_of(ctx, filename: str) -> int:
    conn = ctx.connect()
    try:
        return int(conn.execute("SELECT id FROM photos WHERE filename = ? ORDER BY id LIMIT 1", (filename,)).fetchone()[0])
    finally:
        conn.close()


# ----------------------------------------------------------------- rotation

def test_rotate_box_round_trips():
    box = [0.1, 0.2, 0.4, 0.6]
    for rot in (90, 180, 270):
        turned = box
        for _ in range(360 // math.gcd(rot, 360)):          # 90 and 270 need four turns, 180 two
            turned = rotate_box(turned, rot)
        assert [round(v, 9) for v in turned] == box
    assert rotate_box(box, 90) == pytest.approx([0.4, 0.1, 0.8, 0.4])


def test_rotation_turns_what_memoria_shows_and_never_the_file(ctx, library, client):
    index(ctx, library)
    photo = library / "Trips/Goa/IMG_x0.jpg"                 # 800 x 600
    before = sha(photo)
    pid = pid_of(ctx, "IMG_x0.jpg")
    idx = client.get("/api/photos/index").json()
    ratio0 = idx["ratio"][idx["ids"].index(pid)]
    assert ratio0 > 1

    assert client.post("/api/photos/rotate", json={"photo_ids": [pid], "degrees": 90}).json()["rotated"] == 1
    idx = client.get("/api/photos/index").json()
    i = idx["ids"].index(pid)
    assert idx["rot"][i] == 90 and idx["ratio"][i] == pytest.approx(1 / ratio0, rel=0.01)
    d = client.get(f"/api/photos/{pid}").json()
    assert (d["width"], d["height"], d["rotation"]) == (600, 800, 90)
    for s in ("m", "l"):
        img = Image.open(io.BytesIO(client.get(f"/api/thumb/{pid}", params={"s": s}).content))
        assert img.height > img.width, s
    orig = Image.open(io.BytesIO(client.get(f"/api/photos/{pid}/original").content))
    assert orig.height > orig.width
    assert sha(photo) == before                               # the file is untouched

    client.post("/api/photos/rotate", json={"photo_ids": [pid], "degrees": -90})
    assert client.get(f"/api/photos/{pid}").json()["rotation"] == 0
    assert client.post("/api/photos/rotate", json={"photo_ids": [pid], "degrees": 45}).status_code == 400


# ----------------------------------------------------------------- uploads

def _jpeg(colour=(10, 120, 200), taken: str | None = "2023:08:15 10:30:00") -> bytes:
    import piexif

    img = Image.new("RGB", (640, 480), colour)
    buf = io.BytesIO()
    kw = {}
    if taken:
        kw["exif"] = piexif.dump({"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: taken.encode()}, "GPS": {}})
    img.save(buf, "JPEG", **kw)
    return buf.getvalue()


def test_upload_places_by_date_skips_duplicates_and_never_overwrites(ctx, library, client):
    index(ctx, library)
    a, b = _jpeg((10, 120, 200)), _jpeg((200, 30, 30))
    r = client.post("/api/upload", files=[("files", ("IMG_1.jpg", a, "image/jpeg")),
                                           ("files", ("IMG_1.jpg", b, "image/jpeg")),     # same name, other photo
                                           ("files", ("again.jpg", a, "image/jpeg")),     # same bytes, other name
                                           ("files", ("notes.txt", b"hello", "text/plain"))]).json()
    st = [x["status"] for x in r["results"]]
    assert st == ["added", "added", "duplicate", "rejected"]
    root = ctx.paths.data / "uploads"
    month = root / "Phone" / "2023" / "08"
    assert sorted(p.name for p in month.iterdir()) == ["IMG_1 (2).jpg", "IMG_1.jpg"]
    assert (month / "IMG_1.jpg").read_bytes() == a and (month / "IMG_1 (2).jpg").read_bytes() == b
    assert not list((root / ".incoming").iterdir())                       # no half files left behind

    # after indexing, the same photo is recognised by its bytes; and a photo already in the library too
    Indexer(ctx, workers=2).run(roots=[str(root)])
    again = client.post("/api/upload", files=[("files", ("x.jpg", a, "image/jpeg"))]).json()["results"][0]
    assert again["status"] == "duplicate" and again["photo_id"]
    lib_photo = (library / "Trips/Goa/IMG_x0.jpg").read_bytes()
    assert client.post("/api/upload", files=[("files", ("y.jpg", lib_photo, "image/jpeg"))]).json()["results"][0][
        "status"] == "duplicate"
    conn = ctx.connect()
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE rel_path LIKE 'Phone/2023/08/%'").fetchone()[0] == 2
    conn.close()


def test_upload_without_a_date_goes_to_undated(ctx, client):
    r = client.post("/api/upload", files=[("files", ("scan.jpg", _jpeg(taken=None), "image/jpeg"))]).json()
    assert r["results"][0]["status"] == "added"
    assert (ctx.paths.data / "uploads" / "Phone" / "Undated" / "scan.jpg").exists()


# ----------------------------------------------------------------- backup

def test_backup_copies_everything_and_never_deletes(ctx, library, tmp_path):
    from photointel.engine.backup import last_backup, run_backup

    (library / "Trips/Goa/IMG_x0.jpg.json").write_text("{}")                  # sidecars are backed up too
    index(ctx, library)
    conn = ctx.connect()
    target = tmp_path / "usb"
    r = run_backup(ctx, conn, target)
    base = target / "Memoria Backup"
    lib_files = [p for p in library.rglob("*") if p.is_file()]
    assert r["copied"] == len(lib_files) and r["failed"] == 0 and not r["hash_mismatches"]
    for p in lib_files:
        assert (base / "photos" / library.name / p.relative_to(library)).read_bytes() == p.read_bytes()
    snap = sqlite3.connect(base / "memoria" / "library.db")
    assert snap.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    snap.close()
    assert last_backup(conn)["copied"] == len(lib_files)

    again = run_backup(ctx, conn, target)
    assert again["copied"] == 0 and again["unchanged"] == len(lib_files)
    (library / "Trips/Goa/IMG_x1.jpg").unlink()                                # gone from the library ...
    run_backup(ctx, conn, target)
    assert (base / "photos" / library.name / "Trips/Goa/IMG_x1.jpg").exists()   # ... still in the backup
    conn.close()


def test_backup_reports_a_file_that_no_longer_matches_its_hash(ctx, library, tmp_path):
    from photointel.engine.backup import run_backup

    index(ctx, library)
    p = library / "Trips/Goa/IMG_x2.jpg"
    data = bytearray(p.read_bytes())
    data[-10] ^= 0xFF                                                          # a flipped bit, same size
    st = p.stat()
    p.write_bytes(bytes(data))
    os.utime(p, (st.st_atime, st.st_mtime))
    conn = ctx.connect()
    r = run_backup(ctx, conn, tmp_path / "usb")
    assert [Path(x).name for x in r["hash_mismatches"]] == ["IMG_x2.jpg"]
    conn.close()


def test_backup_refuses_a_folder_inside_the_library_or_the_data(ctx, library, client):
    index(ctx, library)
    assert client.post("/api/backup", json={"folder": str(library / "bk")}).status_code == 400
    assert client.post("/api/backup", json={"folder": str(ctx.paths.data / "bk")}).status_code == 400


# ----------------------------------------------------------------- schedule

def test_schedule_rules():
    from types import SimpleNamespace

    from photointel.scheduler import due

    s = SimpleNamespace(auto_index_minutes=60, backup_folder="E:/bk", backup_every_days=7)
    now = 1_000_000.0
    assert due(s, now, now - 3599, None, False, False) == ["backup"]          # index not yet due; never backed up
    assert due(s, now, now - 3600, now - 86400, False, False) == ["index"]
    assert due(s, now, now - 3600, now - 86400, True, False) == []            # something else is indexing
    assert due(s, now, now - 99999, now - 8 * 86400, False, True) == ["index"]  # a backup is already running
    s.auto_index_minutes, s.backup_folder = 0, ""
    assert due(s, now, 0, None, False, False) == []


def test_registering_a_folder_twice_at_once_is_safe(ctx, tmp_path):
    """Two phone uploads arriving together both register the upload folder (a 500 in a browser run)."""
    import threading

    from photointel.pipeline.scanner import ensure_root

    for attempt in range(15):
        folder = tmp_path / f"f{attempt}"
        folder.mkdir()
        barrier = threading.Barrier(6)
        ids, errors = [], []

        def go():
            conn = ctx.connect()
            try:
                barrier.wait()
                ids.append(ensure_root(conn, folder))
            except Exception as exc:          # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors, errors
        assert len(set(ids)) == 1


def test_parallel_uploads_never_mix_their_bytes(ctx, monkeypatch):
    """Two uploads at once once shared a temp file (named by a coarse clock) and one photo was
    stored with the other's bytes. Freeze the clock so any name built from it collides."""
    import threading

    from photointel.engine import uploads

    monkeypatch.setattr(uploads.time, "time_ns", lambda: 1)
    photos = {f"P{i}.jpg": _jpeg((i * 20 % 255, 90, 200 - i * 10), taken=f"2024:0{1 + i % 9}:10 10:00:00") for i in range(8)}
    barrier = threading.Barrier(len(photos))
    results = {}

    class Slow(io.BytesIO):                      # hand the bytes over in small pieces, as a network does
        def read(self, n=-1):
            return super().read(min(n if n > 0 else 1 << 30, 4096))

    def go(name, data):
        conn = ctx.connect()
        try:
            barrier.wait()
            results[name] = uploads.save_upload(ctx, conn, Slow(data), name)
        finally:
            conn.close()

    threads = [threading.Thread(target=go, args=kv) for kv in photos.items()]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for name, data in photos.items():
        r = results[name]
        assert r.status == "added", (name, r)
        assert hashlib.sha256(Path(r.path).read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest(), name


def test_parallel_uploads_with_the_same_name_all_survive(ctx):
    import threading

    from photointel.engine import uploads

    datas = [_jpeg((10 + i * 30, 60, 90), taken="2024:05:05 10:00:00") for i in range(6)]
    barrier = threading.Barrier(len(datas))
    out = []

    def go(data):
        conn = ctx.connect()
        try:
            barrier.wait()
            out.append((data, uploads.save_upload(ctx, conn, io.BytesIO(data), "IMG_0001.jpg")))
        finally:
            conn.close()

    threads = [threading.Thread(target=go, args=(d,)) for d in datas]
    [t.start() for t in threads]
    [t.join() for t in threads]
    paths = [r.path for _, r in out]
    assert len(set(paths)) == len(datas)                      # six photos, six files
    for data, r in out:
        assert Path(r.path).read_bytes() == data
