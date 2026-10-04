"""The indexer's writer must not lose a photo because someone else committed while it waited.

Seen in an end-to-end run: "DB write failed for photo 25: database is locked", raised at once rather
than after the 60 s busy timeout, leaving the uploaded photo "pending" while the job said Finished.

Cause: the writer thread opened a plain BEGIN before any work arrived, and its very first loop
iteration ran a cancellation check (a read) inside it, which pins a read snapshot. It then waited for
analysis to produce the first result (25 s here, while the models loaded). The job's own progress
heartbeat commits every 5 s on another connection, so by the time the first photo arrived the snapshot
was stale, and SQLite refuses to upgrade a stale read snapshot to a write: instantly, with
"database is locked". The same would happen to any web edit that committed in that window.
"""
from __future__ import annotations

import queue
import threading
import time

import pytest

from photointel.pipeline.indexer import _SENTINEL, Indexer


def build(ctx, library):
    # A job id matters: the writer's periodic cancel check reads the jobs row, and that read (inside the
    # old up-front BEGIN) is what pinned the stale snapshot.
    c = ctx.connect()
    job_id = c.execute("INSERT INTO jobs(kind, status, params, created_at) VALUES ('index','running','{}',0)").lastrowid
    c.commit()
    c.close()
    idx = Indexer(ctx, job_id=job_id, workers=1, enable_faces=False, enable_semantic=False)
    idx.scan([str(library)])
    conn = ctx.connect()
    tasks = idx.pending_tasks(conn)
    conn.close()
    assert tasks, "the fixture library should have photos to analyse"
    return idx, tasks


def status_of(ctx, photo_id):
    c = ctx.connect()
    try:
        return c.execute("SELECT status, meta_version FROM photos WHERE id = ?", (photo_id,)).fetchone()
    finally:
        c.close()


def test_a_commit_by_someone_else_while_the_writer_waits_does_not_lose_the_photo(ctx, library):
    idx, tasks = build(ctx, library)
    good = next(t for t in tasks if t.filename.startswith("IMG_x"))
    result = idx._cpu_stage(good)
    assert result.error is None

    write_q: queue.Queue = queue.Queue()
    writer = threading.Thread(target=idx._writer_loop, args=(write_q, 1), name="test-writer")
    writer.start()
    try:
        time.sleep(1.0)                    # the writer's first loop iteration has run its cancel check by now
        other = ctx.connect()              # the job heartbeat, or a web edit
        other.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('heartbeat_probe', 'x')")
        other.commit()
        other.close()
        write_q.put(result)                # the first analysed photo finally arrives
        write_q.put(_SENTINEL)
    finally:
        writer.join(timeout=30)
    assert not writer.is_alive(), "the writer never finished"

    row = status_of(ctx, good.photo_id)
    assert row["status"] == "ok" and row["meta_version"] is not None, \
        f"the photo was lost: {dict(row)} (it should have been written)"
    c = ctx.connect()
    errors = c.execute("SELECT stage, error FROM processing_errors WHERE photo_id = ?", (good.photo_id,)).fetchall()
    c.close()
    assert not errors, f"a write error was recorded: {[tuple(e) for e in errors]}"


def test_the_writer_holds_no_database_lock_while_it_waits_for_work(ctx, library):
    """The other half: waiting for analysis (model loading can take 25 s) must not hold the write lock
    either, or every edit in the web app stalls for that long."""
    import sqlite3

    idx, tasks = build(ctx, library)
    write_q: queue.Queue = queue.Queue()
    writer = threading.Thread(target=idx._writer_loop, args=(write_q, 1), name="test-writer")
    writer.start()
    try:
        time.sleep(1.2)
        probe = sqlite3.connect(str(ctx.paths.db), timeout=0.25)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            free = True
        except sqlite3.OperationalError:
            free = False
        finally:
            probe.close()
    finally:
        write_q.put(_SENTINEL)
        writer.join(timeout=30)
    assert free, "the idle writer is holding the database write lock"
