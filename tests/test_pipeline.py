"""Scanning and indexing: incremental behaviour, error isolation, resume, moves."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photointel import db
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.scanner import ensure_root, iter_files, scan_root
from tests.conftest import make_image


def index(ctx, roots=None, **kw):
    idx = Indexer(ctx, workers=2, **kw)
    return idx.run(roots=roots)


def test_scan_finds_supported_files_only(tmp_path):
    root = tmp_path / "lib"
    make_image(root / "a.jpg")
    make_image(root / "sub/b.png", fmt="PNG")
    (root / "notes.txt").write_text("hi")
    (root / "empty.jpg").write_bytes(b"")
    (root / "@eaDir").mkdir()
    make_image(root / "@eaDir/thumb.jpg")
    (root / ".hidden").mkdir()
    make_image(root / ".hidden/x.jpg")
    found = {rel for rel, *_ in iter_files(root, [])}
    assert found == {"a.jpg", "sub/b.png"}


def test_index_and_reindex_is_incremental(ctx, library):
    stats = index(ctx, [str(library)])
    assert stats["processed"] >= 12
    conn = ctx.connect()
    ok = conn.execute("SELECT COUNT(*) FROM photos WHERE status='ok'").fetchone()[0]
    err = conn.execute("SELECT COUNT(*) FROM photos WHERE status='error'").fetchone()[0]
    assert ok >= 12 and err == 1          # the broken file is isolated, not fatal
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0] >= 12

    again = index(ctx, [str(library)])
    assert again["processed"] == 0        # nothing changed -> no work
    conn.close()


def test_changed_file_is_reprocessed(ctx, library):
    index(ctx, [str(library)])
    conn = ctx.connect()
    target = library / "DCIM/Camera/IMG_20240300_110000.jpg"
    before = conn.execute("SELECT sha256 FROM photos WHERE rel_path=?",
                          ("DCIM/Camera/IMG_20240300_110000.jpg",)).fetchone()[0]
    make_image(target, size=(640, 480), colour=(10, 220, 90))   # overwrite with new content
    stats = index(ctx, [str(library)])
    assert stats["processed"] == 1
    after = conn.execute("SELECT sha256 FROM photos WHERE rel_path=?",
                         ("DCIM/Camera/IMG_20240300_110000.jpg",)).fetchone()[0]
    assert after != before
    conn.close()


def test_deleted_file_marked_missing_then_restored(ctx, library):
    index(ctx, [str(library)])
    conn = ctx.connect()
    target = library / "Trips/Goa/IMG_x0.jpg"
    data = target.read_bytes()
    target.unlink()
    index(ctx, [str(library)])
    row = conn.execute("SELECT status FROM photos WHERE rel_path=?", ("Trips/Goa/IMG_x0.jpg",)).fetchone()
    assert row["status"] == "missing"
    target.write_bytes(data)
    index(ctx, [str(library)])
    row = conn.execute("SELECT status FROM photos WHERE rel_path=?", ("Trips/Goa/IMG_x0.jpg",)).fetchone()
    assert row["status"] == "ok"
    conn.close()


def test_moved_file_keeps_its_analysis(ctx, library):
    index(ctx, [str(library)])
    conn = ctx.connect()
    old_rel = "Trips/Goa/IMG_x1.jpg"
    row = conn.execute("SELECT id, sha256 FROM photos WHERE rel_path=?", (old_rel,)).fetchone()
    photo_id, sha = row["id"], row["sha256"]
    faces_before = conn.execute("SELECT COUNT(*) FROM faces WHERE photo_id=?", (photo_id,)).fetchone()[0]

    dest = library / "Sorted/2024/moved.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    (library / old_rel).rename(dest)
    index(ctx, [str(library)])

    moved = conn.execute("SELECT id, rel_path, status FROM photos WHERE sha256=? AND status='ok'", (sha,)).fetchone()
    assert moved["rel_path"] == "Sorted/2024/moved.jpg"
    assert moved["id"] == photo_id                    # same row: identity and faces survive
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE photo_id=?", (photo_id,)).fetchone()[0] == faces_before
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE rel_path=?", (old_rel,)).fetchone()[0] == 0
    conn.close()


def test_empty_root_does_not_wipe_index(ctx, tmp_path, library):
    index(ctx, [str(library)])
    conn = ctx.connect()
    root_id = conn.execute("SELECT id FROM roots").fetchone()[0]
    for p in library.rglob("*"):
        if p.is_file():
            p.unlink()
    stats = scan_root(conn, root_id, library, exclude=[])
    assert stats.missing == 0                          # an empty root is treated as unmounted
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE status='missing'").fetchone()[0] == 0
    conn.close()


def test_error_recorded_with_stage(ctx, library):
    index(ctx, [str(library)])
    conn = ctx.connect()
    row = conn.execute("SELECT stage, error FROM processing_errors").fetchone()
    assert row is not None and row["stage"] == "decode"
    photo = conn.execute("SELECT status, error FROM photos WHERE filename='broken.jpg'").fetchone()
    assert photo["status"] == "error" and photo["error"]
    conn.close()


def test_metadata_extracted(ctx, library):
    index(ctx, [str(library)])
    conn = ctx.connect()
    row = conn.execute(
        "SELECT * FROM photos WHERE rel_path='DCIM/Camera/IMG_20240300_110000.jpg'").fetchone()
    assert row["camera_make"] == "samsung"
    assert row["date_source"] in ("exif", "exif_digitized")
    assert row["gps_lat"] == pytest.approx(17.385, abs=0.01)
    assert row["width"] == 800 and row["height"] == 600
    assert row["sha256"] and row["phash"] is not None
    assert row["source_kind"] == "phone"
    conn.close()


def test_model_versions_recorded(ctx, library):
    index(ctx, [str(library)])
    conn = ctx.connect()
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM models")}
    assert {"face", "semantic"} <= kinds
    assert db.active_model_id(conn, "face") is not None
    # every analysed photo records which models produced its data
    row = conn.execute("SELECT faces_model, semantic_model FROM photos WHERE status='ok' LIMIT 1").fetchone()
    assert row["faces_model"] and row["semantic_model"]
    conn.close()


def test_thumbnails_created_and_shared_by_duplicates(ctx, library):
    index(ctx, [str(library)])
    conn = ctx.connect()
    rows = conn.execute("SELECT sha256 FROM photos WHERE status='ok'").fetchall()
    shas = {r["sha256"] for r in rows}
    thumbs = list(ctx.paths.thumbs.rglob("*.webp"))
    assert len(thumbs) == len(shas)      # one thumbnail per distinct content, not per file
    conn.close()


def test_scan_records_timestamp_on_first_run(ctx, library):
    """A fresh root must show when it was scanned, not 'never'."""
    index(ctx, [str(library)])
    conn = ctx.connect()
    row = conn.execute("SELECT last_scan_at FROM roots").fetchone()
    assert row["last_scan_at"] is not None and row["last_scan_at"] > 0
    conn.close()


def test_index_lock_prevents_a_second_run(ctx, library, tmp_path):
    """Two concurrent runs would double GPU load and race on the same rows."""
    from photointel.pipeline.jobs import IndexLock, run_index_job

    lock = IndexLock(ctx.paths.data / "index.lock")
    assert lock.acquire() is True
    other = IndexLock(ctx.paths.data / "index.lock")
    assert other.acquire() is False           # same process is the owner? no: different object, live pid
    out = run_index_job(ctx, roots=[str(library)])
    assert "skipped" in out
    lock.release()
    assert IndexLock(ctx.paths.data / "index.lock").acquire() is True


def test_index_lock_recovers_from_a_dead_owner(ctx):
    from photointel.pipeline.jobs import IndexLock

    path = ctx.paths.data / "index.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("999999999 0", encoding="utf-8")     # pid that cannot exist
    assert IndexLock(path).acquire() is True


def test_heic_photo_indexes_end_to_end(ctx, tmp_path):
    """A HEIC library must index like any other — most phone photos arrive this way."""
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    import numpy as np
    from PIL import Image

    root = tmp_path / "iphone" / "DCIM"
    root.mkdir(parents=True)
    for i in range(3):
        arr = np.zeros((480, 640, 3), np.uint8)
        arr[:, ::2] = 40 + 60 * i
        arr[240:, :] = 90
        Image.fromarray(arr).save(root / f"IMG_{1000 + i}.HEIC", format="HEIF", quality=80)

    stats = index(ctx, [str(tmp_path / "iphone")])
    assert stats["processed"] == 3
    conn = ctx.connect()
    rows = conn.execute(
        "SELECT ext, status, width, height, sha256, phash FROM photos ORDER BY filename").fetchall()
    assert len(rows) == 3
    for ext, status, w, h, sha, phash in rows:
        assert ext == ".heic"
        assert status == "ok"                  # not quarantined as an unreadable file
        assert (w, h) == (640, 480)            # dimensions come from the real decode
        assert sha and phash is not None
    assert conn.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0] == 3
    conn.close()
