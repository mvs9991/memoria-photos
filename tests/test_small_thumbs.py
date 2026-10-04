"""The phone grid's small thumbnail exists before anyone asks for it (engine/thumbs.py)."""
from __future__ import annotations

from PIL import Image
from photointel.engine import thumbs
from photointel.pipeline.indexer import Indexer


def shas(ctx):
    c = ctx.connect()
    try:
        return sorted({r[0] for r in c.execute("SELECT sha256 FROM photos WHERE status = 'ok' AND sha256 IS NOT NULL "
                                               "AND media_type = 'image'")})            # duplicates share a hash
    finally:
        c.close()


def test_indexing_makes_the_small_thumbnail_too(ctx, library):
    Indexer(ctx, workers=2, enable_faces=False, enable_semantic=False).run(roots=[str(library)])
    found = shas(ctx)
    assert found
    missing = [s for s in found if not thumbs.small_path(ctx.paths.thumbs, s).exists()]
    assert not missing, f"{len(missing)} of {len(found)} photos have no small thumbnail after indexing"


def test_the_backfill_fills_gaps_only_while_allowed(ctx, library):
    Indexer(ctx, workers=2, enable_faces=False, enable_semantic=False).run(roots=[str(library)])
    found = shas(ctx)
    thumbs._known.clear()
    for s in found[:5]:                                   # photos indexed before small thumbnails existed
        thumbs.small_path(ctx.paths.thumbs, s).unlink()
    conn = ctx.connect()
    try:
        assert thumbs.backfill(conn, ctx.paths.thumbs, may_run=lambda: False, pause=0) == 0, "it ran while busy"
        assert len(thumbs.missing(conn, ctx.paths.thumbs, 100)) == 5
        assert thumbs.backfill(conn, ctx.paths.thumbs, may_run=lambda: True, pause=0) == 5
        assert thumbs.missing(conn, ctx.paths.thumbs, 100) == []
        assert thumbs.backfill(conn, ctx.paths.thumbs, may_run=lambda: True, pause=0) == 0
    finally:
        conn.close()
    with Image.open(thumbs.small_path(ctx.paths.thumbs, found[0])) as im:
        assert max(im.size) <= thumbs.SMALL_SIDE
