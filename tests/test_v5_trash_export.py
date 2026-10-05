"""Trash (the only way a file is ever removed) and exporting copies of originals."""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photointel.engine import trash
from photointel.engine.export import ExportSpec, export_to_folder
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages

PKG = Path(__file__).resolve().parent.parent / "photointel"


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


def status_of(ctx, pid: int) -> str:
    conn = ctx.connect()
    try:
        return conn.execute("SELECT status FROM photos WHERE id = ?", (pid,)).fetchone()[0]
    finally:
        conn.close()


# ----------------------------------------------------------------- trash

def test_trash_moves_the_file_and_restore_puts_it_back(ctx, library, client):
    photo = library / "Trips/Goa/IMG_x0.jpg"
    sidecar = library / "Trips/Goa/IMG_x0.jpg.supplemental-metadata.json"
    sidecar.write_text('{"title": "x"}')
    other = library / "Trips/Goa/IMG_x0.xmp"            # a bare-stem sidecar is not ours to move
    other.write_text("<x/>")
    before = sha(photo)
    index(ctx, library)
    pid = pid_of(ctx, "IMG_x0.jpg")

    # a confirmation that does not match what was sent deletes nothing
    bad = client.post("/api/trash", json={"photo_ids": [pid], "confirm": 2})
    assert bad.status_code == 400 and photo.exists()

    r = client.post("/api/trash", json={"photo_ids": [pid], "confirm": 1}).json()
    assert r["trashed"] == 1
    assert not photo.exists() and not sidecar.exists() and other.exists()
    moved = list((ctx.paths.data / "trash").rglob("IMG_x0.jpg"))
    assert len(moved) == 1 and sha(moved[0]) == before
    assert pid not in client.get("/api/photos/index").json()["ids"]
    listed = client.get("/api/trash").json()
    assert [i["photo_id"] for i in listed["items"]] == [pid] and listed["days"] == 30
    assert client.get(f"/api/thumb/{pid}", params={"s": "sm"}).status_code == 200   # still viewable

    index(ctx, library)                                  # a re-scan must not call it "missing"
    assert status_of(ctx, pid) == "trashed"

    assert client.post("/api/trash/restore", json={"photo_ids": [pid]}).json()["restored"] == 1
    assert photo.exists() and sha(photo) == before and sidecar.exists()
    assert pid in client.get("/api/photos/index").json()["ids"]
    assert client.get("/api/trash").json()["items"] == []


def test_restore_never_overwrites_a_file_that_took_the_name(ctx, library, client):
    index(ctx, library)
    photo = library / "Trips/Goa/IMG_x1.jpg"
    original = photo.read_bytes()
    pid = pid_of(ctx, "IMG_x1.jpg")
    client.post("/api/trash", json={"photo_ids": [pid], "confirm": 1})
    photo.write_bytes(b"a different file now lives here")
    client.post("/api/trash/restore", json={"photo_ids": [pid]})
    assert photo.read_bytes() == b"a different file now lives here"
    back = library / "Trips/Goa/IMG_x1 (restored).jpg"
    assert back.read_bytes() == original
    conn = ctx.connect()
    assert conn.execute("SELECT rel_path FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "Trips/Goa/IMG_x1 (restored).jpg"
    conn.close()


def test_expired_files_are_erased_and_nothing_else(ctx, library, client):
    index(ctx, library)
    a, b = pid_of(ctx, "IMG_x2.jpg"), pid_of(ctx, "IMG_x3.jpg")
    client.post("/api/trash", json={"photo_ids": [a, b], "confirm": 2})
    conn = ctx.connect()
    conn.execute("UPDATE trash SET expires_at = ? WHERE photo_id = ?", (time.time() - 1, a))
    conn.commit()
    out = trash.purge_expired(ctx, conn)
    assert out["erased"] == 1
    assert not list((ctx.paths.data / "trash").rglob("IMG_x2.jpg"))
    assert list((ctx.paths.data / "trash").rglob("IMG_x3.jpg"))
    assert status_of(ctx, a) == "deleted" and status_of(ctx, b) == "trashed"
    assert conn.execute("SELECT action FROM audit_log WHERE action = 'photos_erased'").fetchone()
    conn.close()

    assert client.post("/api/trash/erase", json={"photo_ids": [b], "confirm": 5}).status_code == 400
    assert client.post("/api/trash/empty", json={"confirm": 7}).status_code == 400
    assert list((ctx.paths.data / "trash").rglob("IMG_x3.jpg"))
    assert client.post("/api/trash/empty", json={"confirm": 1}).json()["erased"] == 1
    assert not list((ctx.paths.data / "trash").rglob("*.jpg"))
    # the photos that were never trashed are untouched
    assert (library / "Trips/Goa/IMG_x0.jpg").exists() and (library / "Trips/Goa/IMG_x4.jpg").exists()


def test_deleting_can_be_switched_off(ctx, library, client):
    index(ctx, library)
    pid = pid_of(ctx, "IMG_x0.jpg")
    client.post("/api/settings", json={"allow_delete": False})
    r = client.post("/api/trash", json={"photo_ids": [pid], "confirm": 1})
    assert r.status_code == 400 and (library / "Trips/Goa/IMG_x0.jpg").exists()


def test_a_live_photo_goes_to_the_trash_with_its_video(ctx, library, client):
    index(ctx, library)
    still, video = pid_of(ctx, "IMG_x0.jpg"), pid_of(ctx, "IMG_x1.jpg")
    conn = ctx.connect()
    conn.execute("UPDATE photos SET live_video_id = ? WHERE id = ?", (video, still))
    conn.execute("UPDATE photos SET live_component = 1 WHERE id = ?", (video,))
    conn.commit()
    conn.close()
    assert client.post("/api/trash", json={"photo_ids": [still], "confirm": 1}).json()["trashed"] == 2
    assert not (library / "Trips/Goa/IMG_x1.jpg").exists()
    assert [i["photo_id"] for i in client.get("/api/trash").json()["items"]] == [still]
    client.post("/api/trash/restore", json={"photo_ids": [still]})
    assert (library / "Trips/Goa/IMG_x0.jpg").exists() and (library / "Trips/Goa/IMG_x1.jpg").exists()


def test_a_trash_folder_inside_the_library_is_never_indexed(ctx, library, client, monkeypatch):
    """Photos on another drive are trashed to <root>/.memoria-trash; the scanner must skip it."""
    monkeypatch.setattr(trash, "_trash_root", lambda ctx_, root, src: root / trash.TRASH_DIR_NAME)
    index(ctx, library)
    conn = ctx.connect()
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    conn.close()
    pid = pid_of(ctx, "IMG_x0.jpg")
    client.post("/api/trash", json={"photo_ids": [pid], "confirm": 1})
    assert list((library / ".memoria-trash").rglob("IMG_x0.jpg"))
    index(ctx, library)
    conn = ctx.connect()
    assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == total
    conn.close()
    assert status_of(ctx, pid) == "trashed"


def test_a_delete_interrupted_by_a_crash_is_undone(ctx, library):
    index(ctx, library)
    pid = pid_of(ctx, "IMG_x0.jpg")
    src = library / "Trips/Goa/IMG_x0.jpg"
    dest = ctx.paths.data / "trash" / "99" / "IMG_x0.jpg"
    dest.parent.mkdir(parents=True)
    src.rename(dest)                                    # the rename happened, the "done" write did not
    conn = ctx.connect()
    conn.execute("INSERT INTO trash(id, photo_id, files, size, prev_status, state, trashed_at, expires_at) "
                 "VALUES (99, ?, ?, 1, 'ok', 'pending', ?, ?)",
                 (pid, json.dumps([[str(src), str(dest)]]), time.time(), time.time() + 9e5))
    conn.commit()
    assert trash.reconcile(conn) == 1
    assert src.exists() and not dest.exists()
    assert conn.execute("SELECT COUNT(*) FROM trash").fetchone()[0] == 0
    conn.close()
    assert status_of(ctx, pid) == "ok"


# Every place the package may remove or move a file, and how many times. A new one fails
# this test on purpose: it has to be looked at, because "no accidental deletes" is the rule.
ALLOWED_REMOVALS = {
    "engine/trash.py": None,            # the trash itself (renames, and erasing expired/emptied files)
    "api/routes_system.py": 1,          # "clear cache": files under <data>/cache only
    "geodata.py": 2,                    # temp download, stale place data under <data>/geo
    "imaging.py": 1,                    # a thumbnail temp file
    "pipeline/jobs.py": 1,              # the index lock file
    "video.py": 2,                      # transcode temp files
    "engine/uploads.py": 1,             # an upload's own temp file in <upload folder>/.incoming
    "engine/editor.py": 1,              # a trim's own .part file when nothing was copied
    "engine/export.py": 1,              # an export copy's own temp file in the export folder when the copy failed
    "engine/thumbpack.py": 3,           # its own cache files: a half-written pack, the pack it replaced, its staging file
    "api/images.py": 2,                 # temp files under <data>/cache: a rotated thumbnail that lost a race; a face crop whose write failed
    "api/dav.py": 1,                    # _discard(): a backup app's own temp upload in .incoming (too large / dropped / stored / moved / replaced)
    "autostart.py": 3,                  # its own start-up entries (Startup .cmd, launchd plist, systemd unit)
    "tls.py": 1,                        # a half-fetched certificate in <data>/tls
}
REMOVAL = re.compile(r"\.unlink\(|os\.remove\(|os\.unlink\(|rmtree\(|\.rmdir\(|os\.rename\(|\.rename\(|send2trash")


def test_only_the_trash_can_remove_a_file():
    found: dict[str, int] = {}
    for f in PKG.rglob("*.py"):
        rel = f.relative_to(PKG).as_posix()
        code = "\n".join(l for l in f.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("#"))
        n = len(REMOVAL.findall(code))
        if n:
            found[rel] = n
    for rel, n in found.items():
        assert rel in ALLOWED_REMOVALS, f"{rel} removes or moves files; review it and add it here if it is safe"
        if ALLOWED_REMOVALS[rel] is not None:
            assert n == ALLOWED_REMOVALS[rel], f"{rel}: {n} removal calls, expected {ALLOWED_REMOVALS[rel]}"


def test_only_a_users_click_can_trash():
    """Indexing, clustering, duplicates, clean-up lists and the Claude layer cannot reach the trash."""
    callers = {}
    for f in PKG.rglob("*.py"):
        rel = f.relative_to(PKG).as_posix()
        text = f.read_text(encoding="utf-8")
        if rel != "engine/trash.py" and re.search(r"\bmove_to_trash\(|\bpurge\(|\brestore\(", text) \
                and re.search(r"engine import trash|engine\.trash|from \.trash|import trash", text):
            callers[rel] = sorted(set(re.findall(r"trash\.(\w+)\(", text)))
    assert set(callers) <= {"api/routes_trash.py", "cli.py", "api/app.py"}, callers
    assert "move_to_trash" in callers.get("api/routes_trash.py", [])
    assert "move_to_trash" not in callers.get("cli.py", []) and "move_to_trash" not in callers.get("api/app.py", [])
    assert set(callers.get("api/app.py", [])) <= {"reconcile", "purge_expired"}


# ----------------------------------------------------------------- export

def _people_truth(ctx) -> dict[int, set[int]]:
    conn = ctx.connect()
    out: dict[int, set[int]] = {}
    for pid, photo in conn.execute(
            """SELECT f.person_id, f.photo_id FROM faces f JOIN photos p ON p.id = f.photo_id
               WHERE f.person_id IS NOT NULL AND p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0"""):
        out.setdefault(int(pid), set()).add(int(photo))
    conn.close()
    return out


def test_export_people_a_folder_each_or_together(ctx, library, tmp_path):
    ctx.test_face_engine.faces_per_image = {1: 2}        # the DCIM photos show two people side by side
    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["people"])
    truth = _people_truth(ctx)
    people = sorted(truth, key=lambda p: -len(truth[p]))[:3]
    assert len(people) == 3
    from photointel.engine.people import person_label
    labels = {p: person_label(conn.execute("SELECT * FROM persons WHERE id=?", (p,)).fetchone()) for p in people}

    out = tmp_path / "out_each"
    r = export_to_folder(conn, ExportSpec(person_ids=people, person_mode="each", layout="flat", folder=str(out)))
    for p in people:
        files = [f for f in (out / labels[p]).iterdir() if f.suffix == ".jpg"]
        # identical copies (the Backup twin of a DCIM photo) are written once per folder
        n_distinct = len({conn.execute("SELECT sha256 FROM photos WHERE id=?", (x,)).fetchone()[0] for x in truth[p]})
        assert len(files) == n_distinct, labels[p]
    assert r["failed"] == 0

    from itertools import combinations

    from photointel.engine.export import plan
    # every pair: two who always appear together, and pairs who never do (so "together" != "any")
    pairs = list(combinations(people, 2))
    assert any(truth[a] & truth[b] for a, b in pairs) and any(not truth[a] & truth[b] for a, b in pairs)
    for a, b in pairs:
        spec = ExportSpec(person_ids=[a, b], person_mode="together", folder=str(tmp_path / "t"))
        assert {i.photo_id for i in plan(conn, spec).items} == truth[a] & truth[b]
    anyof = set.union(*(truth[p] for p in people))
    spec_any = ExportSpec(person_ids=people, person_mode="any", folder=str(tmp_path / "x"))
    assert {i.photo_id for i in plan(conn, spec_any).items} == anyof
    conn.close()


def test_export_copies_are_exact_and_originals_untouched(ctx, library, tmp_path):
    index(ctx, library)
    originals = {p: sha(p) for p in library.rglob("*") if p.is_file()}
    mtimes = {p: p.stat().st_mtime for p in originals}
    conn = ctx.connect()
    out = tmp_path / "by_year"
    r = export_to_folder(conn, ExportSpec(year=2024, month=7, layout="date", xmp=True, folder=str(out)))
    assert r["copied"] == 5 and r["failed"] == 0
    for i in range(5):
        copy = out / "2024" / "07" / f"IMG_x{i}.jpg"
        src = library / "Trips/Goa" / f"IMG_x{i}.jpg"
        assert sha(copy) == sha(src) and abs(copy.stat().st_mtime - src.stat().st_mtime) < 2
        assert (out / "2024" / "07" / f"IMG_x{i}.jpg.xmp").read_text(encoding="utf-8").startswith("<?xpacket")
    again = export_to_folder(conn, ExportSpec(year=2024, month=7, layout="date", folder=str(out)))
    assert again["copied"] == 0 and again["already_there"] == 5          # a re-run resumes, never duplicates
    assert {p: sha(p) for p in originals} == originals
    assert {p: p.stat().st_mtime for p in originals} == mtimes
    assert json.loads((out / "memoria-copies.json").read_text(encoding="utf-8"))["already_there"] == 5
    conn.close()


def test_export_keeps_different_files_with_the_same_name(ctx, library, tmp_path):
    (library / "Other").mkdir()
    from tests.conftest import make_image
    from datetime import datetime
    make_image(library / "Other" / "IMG_x0.jpg", colour=(10, 200, 10), taken=datetime(2024, 7, 20, 12))
    index(ctx, library)
    conn = ctx.connect()
    ids = [int(r[0]) for r in conn.execute("SELECT id FROM photos WHERE filename = 'IMG_x0.jpg'")]
    out = tmp_path / "flat"
    export_to_folder(conn, ExportSpec(photo_ids=ids, layout="flat", folder=str(out)))
    assert sorted(f.name for f in out.glob("IMG_x0*.jpg")) == ["IMG_x0 (2).jpg", "IMG_x0.jpg"]
    assert sha(out / "IMG_x0.jpg") != sha(out / "IMG_x0 (2).jpg")
    conn.close()


def test_export_refuses_a_folder_inside_the_library(ctx, library, client):
    index(ctx, library)
    r = client.post("/api/export", json={"year": 2024, "folder": str(library / "exports")})
    assert r.status_code == 400 and not (library / "exports").exists()


def test_export_api_preview_job_and_zip(ctx, library, client, tmp_path):
    index(ctx, library)
    ids = client.get("/api/photos/index", params={"year": 2024, "month": 7}).json()["ids"]
    pre = client.post("/api/export/preview", json={"photo_ids": ids}).json()
    assert pre["items"] == 5 and pre["bytes"] > 0

    out = tmp_path / "job"
    job = client.post("/api/export", json={"photo_ids": ids, "layout": "flat", "folder": str(out)}).json()["job_id"]
    for _ in range(100):
        conn = ctx.connect()
        st = conn.execute("SELECT status, message FROM jobs WHERE id = ?", (job,)).fetchone()
        conn.close()
        if st[0] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    assert st[0] == "done", st
    assert len(list(out.glob("*.jpg"))) == 5

    z = client.post("/api/export/zip", data={"spec": json.dumps({"photo_ids": ids, "layout": "date"})})
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    assert zf.testzip() is None
    names = sorted(zf.namelist())
    assert names == [f"2024/07/IMG_x{i}.jpg" for i in range(5)]
    assert zf.read("2024/07/IMG_x3.jpg") == (library / "Trips/Goa/IMG_x3.jpg").read_bytes()
    bad = client.post("/api/export/zip", data={"spec": json.dumps({"layout": "date"})})
    assert bad.status_code == 400


def test_export_skips_hidden_photos_unless_chosen(ctx, library, client, tmp_path):
    index(ctx, library)
    pid = pid_of(ctx, "IMG_x0.jpg")
    client.post("/api/photos/hide", json={"photo_ids": [pid]})
    conn = ctx.connect()
    from photointel.engine.export import plan
    assert pid not in {i.photo_id for i in plan(conn, ExportSpec(year=2024, month=7)).items}
    assert pid in {i.photo_id for i in plan(conn, ExportSpec(photo_ids=[pid])).items}
    conn.close()


def test_a_running_export_does_not_block_indexing(ctx, monkeypatch):
    from photointel.pipeline import jobs

    conn = ctx.connect()
    export_job = jobs.create_job(conn, "export", {})
    conn.execute("UPDATE jobs SET status='running', heartbeat_at=? WHERE id=?", (time.time(), export_job))
    conn.commit()
    conn.close()
    spawned = []
    monkeypatch.setattr(jobs, "_spawn", lambda args: spawned.append(args))
    new = jobs.spawn_index_job(ctx, {"kind": "index", "post_only": True})
    assert new != export_job and spawned


def test_person_counts_follow_the_trash(ctx, library, client):
    index(ctx, library)
    conn = ctx.connect()
    run_post_stages(ctx, conn, stages=["people"])
    pid = pid_of(ctx, "IMG_x0.jpg")
    person = conn.execute("SELECT person_id FROM faces WHERE photo_id = ? AND person_id IS NOT NULL", (pid,)).fetchone()[0]
    count = lambda: conn.execute("SELECT photo_count FROM persons WHERE id = ?", (person,)).fetchone()[0]  # noqa: E731
    before = count()
    assert client.post("/api/trash", json={"photo_ids": [pid], "confirm": 1}).status_code == 200
    assert count() == before - 1
    assert client.post("/api/trash/restore", json={"photo_ids": [pid]}).status_code == 200
    assert count() == before
    conn.close()


def test_exported_copies_carry_the_capture_date_not_the_file_date(ctx, library, tmp_path):
    """A copy must be stamped with when the photo was *taken*.

    Whoever receives the folder — another computer, a phone, a pen drive — sorts by the
    file's modification time. If that is when the file happened to be written, or a wrong
    camera clock Memoria has since corrected, the photos land in the wrong order.
    """
    import os
    from datetime import datetime

    from photointel.metadata import ts_to_naive

    index(ctx, library)
    conn = ctx.connect()
    src = library / "Trips/Goa" / "IMG_x0.jpg"
    pid, taken = conn.execute(
        "SELECT id, taken_ts FROM photos WHERE rel_path LIKE '%IMG_x0.jpg'").fetchone()
    assert taken, "fixture photo should have a capture date"

    # The file's own mtime is wrong — as it is after a copy, a download or a bad clock.
    wrong = datetime(2001, 1, 1, 9, 0).timestamp()
    os.utime(src, (wrong, wrong))
    assert abs(src.stat().st_mtime - wrong) < 2

    out = tmp_path / "stamped"
    r = export_to_folder(conn, ExportSpec(photo_ids=[pid], layout="flat", folder=str(out)))
    assert r["copied"] == 1
    copy = out / "IMG_x0.jpg"
    # taken_ts is wall-clock encoded as UTC, so the expected mtime goes back through
    # the local zone — exactly what the export does.
    want = ts_to_naive(taken).timestamp()
    assert abs(copy.stat().st_mtime - want) < 2, "copy should carry the capture date"
    assert abs(copy.stat().st_mtime - wrong) > 60, "copy should not carry the wrong file date"
    assert sha(copy) == sha(src)                       # still byte-exact
    assert abs(src.stat().st_mtime - wrong) < 2        # the original is left alone

    # And a re-run still recognises it, rather than copying everything again.
    again = export_to_folder(conn, ExportSpec(photo_ids=[pid], layout="flat", folder=str(out)))
    assert again["copied"] == 0 and again["already_there"] == 1
    conn.close()


def test_zip_entries_also_carry_the_capture_date(ctx, library, tmp_path):
    from photointel.engine.export import iter_zip
    from photointel.metadata import ts_to_naive

    index(ctx, library)
    conn = ctx.connect()
    pid, taken = conn.execute(
        "SELECT id, taken_ts FROM photos WHERE rel_path LIKE '%IMG_x0.jpg'").fetchone()
    blob = b"".join(iter_zip(conn, ExportSpec(photo_ids=[pid], layout="flat")))
    zf = zipfile.ZipFile(io.BytesIO(blob))
    info = zf.infolist()[0]
    want = ts_to_naive(taken)                 # the wall clock the photo was taken at
    assert info.date_time[:5] == (want.year, want.month, want.day, want.hour, want.minute)
    conn.close()


def test_an_export_never_replaces_another_apps_sidecar(ctx, library, tmp_path):
    """Exporting with XMP into a folder that already holds the photo and a Lightroom sidecar for it."""
    from photointel.engine import export as export_mod
    from photointel.pipeline.indexer import Indexer

    Indexer(ctx, workers=1).run(roots=[str(library)])
    conn = ctx.connect()
    pid = conn.execute("SELECT id FROM photos WHERE status = 'ok' AND live_component = 0 ORDER BY id LIMIT 1").fetchone()[0]
    out = tmp_path / "out"
    spec = export_mod.ExportSpec(photo_ids=[pid], layout="flat", xmp=True, folder=str(out))
    export_mod.export_to_folder(conn, spec)
    side = next(out.glob("*.xmp"))
    side.write_text("<x:xmpmeta x:xmptk='Adobe XMP Core'>Lightroom's edits</x:xmpmeta>", encoding="utf-8")
    export_mod.export_to_folder(conn, spec)            # a re-run into the same folder
    assert "Lightroom's edits" in side.read_text(encoding="utf-8")     # was: replaced with Memoria's
