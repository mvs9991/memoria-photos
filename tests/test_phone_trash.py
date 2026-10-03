"""An Android phone's own trash must not appear in the library as ordinary photos.

When you delete a photo in Android's gallery it is renamed ".trashed-<expiry>-<name>" and
kept for about 30 days. A backup of the phone's DCIM folder carries those files along, and on a
real library 94 of them (75 photos, 19 videos) were showing up as normal photos, some of them
among the newest in the timeline. The person deleted them on purpose.

They are hidden, not skipped: skipping would flip every one to "missing" (which the app reads as
an unplugged drive) and would lose anything a person still wants back. Hidden ones stay one
"Show again" away in Collections -> Hidden, and the files are never touched.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from photointel import db
from photointel.pipeline.indexer import Indexer
from tests.conftest import make_image

TRASHED = ".trashed-1747532861-IMG20250418071734.jpg"


@pytest.fixture
def phone(tmp_path):
    root = tmp_path / "phone"
    make_image(root / "DCIM/Camera/IMG_kept.jpg", colour=(40, 90, 160), taken=datetime(2025, 4, 18, 7, 0), noise=14)
    make_image(root / f"DCIM/Camera/{TRASHED}", colour=(200, 80, 60), taken=datetime(2025, 4, 18, 7, 17), noise=14)
    make_image(root / "DCIM/Camera/.pending-1747532999-IMG_unfinished.jpg", colour=(30, 160, 90),
               taken=datetime(2025, 4, 18, 7, 30), noise=14)
    make_image(root / "DCIM/Camera/.my_private_name.jpg", colour=(120, 120, 30),
               taken=datetime(2025, 4, 18, 7, 40), noise=14)
    return root


def hidden_by_name(ctx) -> dict[str, int]:
    conn = ctx.connect()
    try:
        return {r["filename"]: r["hidden"] for r in conn.execute("SELECT filename, hidden FROM photos")}
    finally:
        conn.close()


def index(ctx, root):
    return Indexer(ctx, workers=2).run(roots=[str(root)])


def test_a_phone_trash_file_arrives_hidden_and_nothing_else_does(ctx, phone):
    index(ctx, phone)
    h = hidden_by_name(ctx)
    assert h[TRASHED] == 1
    assert h["IMG_kept.jpg"] == 0
    # Only ".trashed-" means "deleted". ".pending-" can be the sole copy of a capture that never
    # finished renaming, and a person's own dot-named photo is theirs.
    assert h[".pending-1747532999-IMG_unfinished.jpg"] == 0
    assert h[".my_private_name.jpg"] == 0


def test_it_is_out_of_the_library_but_recoverable_from_hidden(ctx, phone):
    from photointel.api.app import create_app

    index(ctx, phone)
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    conn = ctx.connect()
    trashed_id = conn.execute("SELECT id FROM photos WHERE filename = ?", (TRASHED,)).fetchone()[0]
    conn.close()

    shown = client.get("/api/photos/index").json()["ids"]
    assert trashed_id not in shown and len(shown) == 3
    in_hidden = client.get("/api/photos/index", params={"collection": "hidden"}).json()["ids"]
    assert in_hidden == [trashed_id]

    # "Show again" works and brings it back into the library.
    assert client.post("/api/photos/hide", json={"photo_ids": [trashed_id], "hidden": False}).json()["changed"] == 1
    assert trashed_id in client.get("/api/photos/index").json()["ids"]


def test_showing_it_again_survives_the_next_scan(ctx, phone):
    index(ctx, phone)
    conn = ctx.connect()
    conn.execute("UPDATE photos SET hidden = 0 WHERE filename = ?", (TRASHED,))
    conn.commit()
    conn.close()
    index(ctx, phone)
    assert hidden_by_name(ctx)[TRASHED] == 0


def test_the_files_themselves_are_never_touched(ctx, phone):
    before = {p.name: p.read_bytes() for p in phone.rglob("*.jpg")}
    index(ctx, phone)
    assert {p.name: p.read_bytes() for p in phone.rglob("*.jpg")} == before


def test_nothing_goes_missing_which_the_app_would_read_as_an_unplugged_drive(ctx, phone):
    index(ctx, phone)
    index(ctx, phone)
    conn = ctx.connect()
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE status = 'missing'").fetchone()[0] == 0
    conn.close()


def test_a_library_indexed_before_this_existed_is_cleaned_up_exactly_once(ctx, phone):
    """What happened to the real library: 94 already indexed, visible, and flagged by nothing."""
    index(ctx, phone)
    conn = ctx.connect()
    conn.execute("UPDATE photos SET hidden = 0")                       # as an older version left it
    conn.execute("DELETE FROM meta WHERE key = 'phone_trash_hidden'")
    conn.commit()
    conn.close()

    db.init_db(ctx.paths.db).close()                                   # the next start
    assert hidden_by_name(ctx)[TRASHED] == 1
    assert hidden_by_name(ctx)["IMG_kept.jpg"] == 0

    # Once only: a person who then chooses "Show again" must not have it undone on the next start.
    conn = ctx.connect()
    conn.execute("UPDATE photos SET hidden = 0 WHERE filename = ?", (TRASHED,))
    conn.commit()
    conn.close()
    db.init_db(ctx.paths.db).close()
    assert hidden_by_name(ctx)[TRASHED] == 0


def test_the_cleanup_is_recorded_in_the_audit_log(ctx, phone):
    index(ctx, phone)
    conn = ctx.connect()
    conn.execute("UPDATE photos SET hidden = 0")
    conn.execute("DELETE FROM meta WHERE key = 'phone_trash_hidden'")
    conn.commit()
    conn.close()
    db.init_db(ctx.paths.db).close()
    conn = ctx.connect()
    row = conn.execute("SELECT actor, details FROM audit_log WHERE action = 'phone_trash_hidden'").fetchone()
    conn.close()
    assert row is not None and row["actor"] == "system" and '"photos": 1' in row["details"]
