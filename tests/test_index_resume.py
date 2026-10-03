"""Interrupted indexing: a mid-run cancel and a hard stop, then a resume. Pins that nothing is
duplicated, stuck 'pending', or left as a half-written thumbnail."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from photointel import imaging
from photointel.pipeline.indexer import CancelledError, Indexer
from tests.conftest import make_image

N = 30
REPO = Path(__file__).resolve().parent.parent


def _lib(tmp_path) -> Path:
    root = tmp_path / "lib"
    for i in range(N):
        make_image(root / "a" / f"IMG_{i:03d}.jpg", colour=(30 + 6 * i, 90, 160),
                   taken=datetime(2024, 3, 9, 11, 0) + timedelta(minutes=i))
    return root


def _check_consistent(ctx, expected=N):
    conn = ctx.connect()
    try:
        rows = conn.execute("SELECT rel_path, status, meta_version, sha256 FROM photos").fetchall()
        assert len(rows) == expected
        assert len({r["rel_path"] for r in rows}) == expected          # no duplicate rows
        assert {r["status"] for r in rows} == {"ok"}, [tuple(r) for r in rows if r["status"] != "ok"]
        assert all(r["meta_version"] is not None and r["sha256"] for r in rows)
        # every photo has a real, decodable thumbnail
        for r in rows:
            tp = imaging.thumb_path(ctx.paths.thumbs, r["sha256"])
            assert tp.exists() and tp.stat().st_size > 0, r["rel_path"]
            with Image.open(tp) as im:
                im.load()
        assert conn.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0] == expected
    finally:
        conn.close()


def test_cancel_midway_then_resume_completes(ctx, tmp_path):
    root = _lib(tmp_path)
    idx = Indexer(ctx, workers=2)
    fake = ctx.test_face_engine
    orig = fake.analyze

    def analyze_then_cancel(*a, **k):
        if fake.calls >= 5:
            idx.cancel()
        return orig(*a, **k)

    fake.analyze = analyze_then_cancel
    with pytest.raises(CancelledError):
        idx.run(roots=[str(root)])
    conn = ctx.connect()
    mid = conn.execute("SELECT status, COUNT(*) FROM photos GROUP BY status").fetchall()
    conn.close()
    statuses = {r[0] for r in mid}
    assert statuses <= {"ok", "pending"}, statuses          # nothing in a made-up state
    assert sum(r[1] for r in mid) == N
    assert "pending" in statuses, "the cancel came too late to interrupt anything"

    fake.analyze = orig
    stats = Indexer(ctx, workers=2).run(roots=[str(root)])    # rescans too: must not duplicate rows
    assert stats["errors"] == 0
    _check_consistent(ctx)
    again = Indexer(ctx, workers=2).run(roots=[str(root)])
    assert again["processed"] == 0                            # and the resume left nothing undone


def test_hard_stop_then_resume_completes(ctx, tmp_path):
    """The process dies (os._exit) after some photos were analysed but before they were committed."""
    root = _lib(tmp_path)
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        from tests.conftest import FakeFaceEngine, FakeSemanticModel
        from photointel.context import AppContext
        from photointel.pipeline import indexer as ix
        ctx = AppContext({str(ctx.paths.data)!r})
        ctx._device = "cpu"
        ctx.settings.ocr_enabled = False
        f, s = FakeFaceEngine(), FakeSemanticModel()
        ctx.face_engine = lambda: f
        ctx.semantic_model = lambda: s
        orig = ix.Indexer._write_result
        n = [0]
        def die(self, conn, r):
            orig(self, conn, r)
            n[0] += 1
            if n[0] == 8:
                os._exit(3)          # no commit, no cleanup: a power cut
        ix.Indexer._write_result = die
        ix.Indexer(ctx, workers=2).run(roots=[{str(root)!r}])
    """)
    p = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=300)
    assert p.returncode == 3, p.stderr[-2000:]
    conn = ctx.connect()
    got = conn.execute("SELECT status, COUNT(*) FROM photos GROUP BY status").fetchall()
    conn.close()
    assert {r[0] for r in got} <= {"ok", "pending"} and sum(r[1] for r in got) == N
    assert "pending" in {r[0] for r in got}

    stats = Indexer(ctx, workers=2).run(roots=[str(root)])
    assert stats["errors"] == 0
    _check_consistent(ctx)


def test_thumbnail_write_is_atomic(tmp_path, monkeypatch):
    """A save that dies half way must not leave a file at the final path (it would never be redone)."""
    dest = tmp_path / "th" / "ab" / "abc.webp"
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    real_save = Image.Image.save

    def partial_save(self, fp, *a, **k):
        Path(fp).write_bytes(b"RIFF-truncated")
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", partial_save)
    with pytest.raises(OSError):
        imaging.save_thumbnail(img, dest, 256)
    assert not dest.exists()
    monkeypatch.setattr(Image.Image, "save", real_save)
    imaging.save_thumbnail(img, dest, 256)
    with Image.open(dest) as im:
        im.load()
        assert max(im.size) == 256
