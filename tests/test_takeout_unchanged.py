"""A Takeout sidecar that has not changed since it was read is not read again, and one that has is."""
from __future__ import annotations

import builtins
import json
import os

import pytest

from photointel.engine import takeout as takeout_mod
from photointel.pipeline.indexer import Indexer


@pytest.fixture
def indexed(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    return ctx, library


def _write(sidecar, description):
    sidecar.write_text(json.dumps({"title": sidecar.name, "description": description,
                                   "photoTakenTime": {"timestamp": "1721466000"}}), encoding="utf-8")


def _reads(monkeypatch):
    opened = []
    real_open = builtins.open

    def counting(file, *a, **kw):
        if str(file).endswith(".json"):
            opened.append(str(file))
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", counting)
    return opened


def test_an_unchanged_sidecar_is_applied_without_being_read_again(indexed, monkeypatch):
    ctx, library = indexed
    photo = library / "Trips/Goa/IMG_x0.jpg"
    sidecar = photo.with_name(photo.name + ".supplemental-metadata.json")
    _write(sidecar, "first words")
    conn = ctx.connect()
    assert takeout_mod.import_takeout(ctx, conn)["sidecars"] == 1
    pid = conn.execute("SELECT id FROM photos WHERE filename = 'IMG_x0.jpg'").fetchone()[0]
    assert conn.execute("SELECT description FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "first words"

    # The description is cleared here; the next run puts it back from what it stored, as reading would.
    conn.execute("UPDATE photos SET description = NULL WHERE id = ?", (pid,))
    conn.commit()
    opened = _reads(monkeypatch)
    out = takeout_mod.import_takeout(ctx, conn)
    assert out["unchanged"] == 1 and not [p for p in opened if p.endswith("supplemental-metadata.json")]
    assert conn.execute("SELECT description FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "first words"

    # Changed on disk: read again.
    _write(sidecar, "second words, longer")
    st = sidecar.stat()
    os.utime(sidecar, (st.st_atime, st.st_mtime + 5))
    conn.execute("UPDATE photos SET description = NULL WHERE id = ?", (pid,))
    conn.commit()
    out = takeout_mod.import_takeout(ctx, conn)
    assert "unchanged" not in out
    assert conn.execute("SELECT description FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "second words, longer"
