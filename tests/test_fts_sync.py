"""The keyword index is brought up to date by writing only what changed, and ends up the same as a rebuild."""
from __future__ import annotations

from photointel import db
from photointel.pipeline.post import _sync_fts


def test_sync_writes_only_the_differences_and_matches_a_rebuild(tmp_path):
    conn = db.init_db(tmp_path / "library.db")
    conn.executemany("INSERT INTO photo_fts(rowid, text) VALUES (?,?)",
                     [(1, "goa beach"), (2, "old words"), (3, "gone photo")])
    changed = _sync_fts(conn, "photo_fts", [(1, "goa beach"), (2, "new words"), (4, "added photo")], batch=2)
    assert changed == 4            # 2 out, 3 out, 2 in, 4 in; 1 untouched
    assert dict(conn.execute("SELECT rowid, text FROM photo_fts")) == {1: "goa beach", 2: "new words",
                                                                       4: "added photo"}
    assert [r[0] for r in conn.execute("SELECT rowid FROM photo_fts WHERE photo_fts MATCH 'words'")] == [2]
    assert not list(conn.execute("SELECT rowid FROM photo_fts WHERE photo_fts MATCH 'old OR gone'"))
    assert _sync_fts(conn, "photo_fts", [(1, "goa beach"), (2, "new words"), (4, "added photo")], batch=2) == 0
