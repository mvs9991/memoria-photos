"""Hidden photos are not part of duplicate detection.

On a real library, 16 duplicate groups had a *hidden* photo as the copy to keep, and 11 groups had
no visible member at all. The cause was that detection looked at every photo, hidden or not, so
a phone's or Google's trash (hidden on purpose) could be chosen as "the one to keep" and the copy
you can actually see was labelled the redundant one, with a button offering to hide or trash it.
"""
from __future__ import annotations

import pytest

from photointel.engine.duplicates import find_duplicates
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages

ORIGINAL = "DCIM/Camera/IMG_20240300_110000.jpg"
COPY = "Backup/IMG_20240300_110000.jpg"          # byte-identical, planted by the `library` fixture


@pytest.fixture
def indexed(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    return ctx, conn


def exact_groups(conn) -> list[list[str]]:
    out = []
    for g in conn.execute("SELECT id FROM dup_groups WHERE kind = 'exact' ORDER BY id"):
        out.append(sorted(r[0] for r in conn.execute(
            "SELECT p.rel_path FROM dup_members m JOIN photos p ON p.id = m.photo_id WHERE m.group_id = ?", (g[0],))))
    return out


def set_hidden(conn, rel_path: str, hidden: int) -> None:
    conn.execute("UPDATE photos SET hidden = ? WHERE rel_path = ?", (hidden, rel_path))
    conn.commit()


def test_the_fixture_really_has_an_exact_pair(indexed):
    ctx, conn = indexed
    assert exact_groups(conn) == [sorted([ORIGINAL, COPY])]


def test_hiding_one_copy_leaves_nothing_to_deduplicate(indexed):
    ctx, conn = indexed
    set_hidden(conn, COPY, 1)
    find_duplicates(ctx, conn)
    assert exact_groups(conn) == []


def test_a_hidden_photo_is_never_the_one_kept(indexed):
    """Hide the original — the copy the engine would normally prefer to keep. The pair must
    dissolve rather than keep a hidden photo and mark the visible one redundant."""
    ctx, conn = indexed
    set_hidden(conn, ORIGINAL, 1)
    find_duplicates(ctx, conn)
    assert exact_groups(conn) == []
    bad = conn.execute(
        "SELECT COUNT(*) FROM dup_groups g JOIN photos p ON p.id = g.keep_photo_id WHERE p.hidden = 1").fetchone()[0]
    assert bad == 0


def test_no_group_ever_contains_a_hidden_photo(indexed):
    ctx, conn = indexed
    conn.execute("UPDATE photos SET hidden = 1 WHERE rel_path LIKE 'Trips/%'")
    conn.commit()
    find_duplicates(ctx, conn)
    hidden_members = conn.execute(
        "SELECT COUNT(*) FROM dup_members m JOIN photos p ON p.id = m.photo_id WHERE p.hidden = 1").fetchone()[0]
    assert hidden_members == 0


def test_showing_the_photo_again_brings_the_group_back(indexed):
    ctx, conn = indexed
    set_hidden(conn, COPY, 1)
    find_duplicates(ctx, conn)
    assert exact_groups(conn) == []
    set_hidden(conn, COPY, 0)
    find_duplicates(ctx, conn)
    assert exact_groups(conn) == [sorted([ORIGINAL, COPY])]


def test_nothing_visible_does_not_erase_earlier_groups(indexed):
    """An empty result is also what an unplugged drive looks like. It must not wipe the groups
    (and with them the user's review decisions)."""
    ctx, conn = indexed
    before = exact_groups(conn)
    assert before
    conn.execute("UPDATE photos SET hidden = 1")
    conn.commit()
    out = find_duplicates(ctx, conn)
    assert out == {"groups": 0, "new_groups": 0}
    assert exact_groups(conn) == before
