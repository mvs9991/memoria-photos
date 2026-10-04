"""One failed write must not break every later write on that API worker.

Seen on the live server: GET /api/jobs answered 500 "database is locked" instantly, on every call.
The jobs list tidies stale jobs with an UPDATE; once that UPDATE lost a race for the write lock,
Python's sqlite3 had already opened an implicit transaction and nothing rolled it back. The worker
thread's connection then sat in that transaction for good, its read snapshot going stale, and SQLite
refuses to upgrade a stale snapshot to a write — at once, with "database is locked". Every later write
on that thread failed the same way (a favourite, a delete), until the server restarted.
"""
from __future__ import annotations

import sqlite3
import time

from photointel.api.deps import ApiState, start_request
from photointel.pipeline import jobs


def test_a_transaction_an_earlier_request_left_open_does_not_poison_the_next(ctx):
    state = ApiState(ctx)
    start_request()
    c = state.conn()
    c.execute("BEGIN")
    c.execute("SELECT COUNT(*) FROM photos").fetchone()      # the stale snapshot is pinned here
    # ...the request fails without rolling back. Meanwhile someone else commits.
    other = ctx.connect()
    other.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('probe', '1')")
    other.commit()
    other.close()

    start_request()                                          # the next request on this same thread
    c2 = state.conn()
    assert c2 is c, "the test relies on the thread reusing its connection"
    c2.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('probe', '2')")
    c2.commit()
    assert c2.execute("SELECT value FROM meta WHERE key = 'probe'").fetchone()[0] == "2"


def test_listing_jobs_takes_no_write_lock_when_nothing_is_stale(ctx):
    c = ctx.connect()
    c.execute("PRAGMA busy_timeout = 200")
    holder = ctx.connect()
    holder.execute("BEGIN IMMEDIATE")                        # an index job mid-write
    try:
        t = time.time()
        assert jobs.reap_stale_jobs(c) == 0
        assert time.time() - t < 0.15, "it waited for the write lock"
    finally:
        holder.rollback()
        holder.close()
    assert not c.in_transaction
    c.close()


def test_a_reap_that_loses_the_race_leaves_no_transaction_behind(ctx):
    c = ctx.connect()
    c.execute("INSERT INTO jobs(kind, status, params, created_at, heartbeat_at) VALUES ('index','running','{}',0,0)")
    c.commit()
    c.execute("PRAGMA busy_timeout = 100")
    holder = ctx.connect()
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert jobs.reap_stale_jobs(c) == 0                   # best effort: the next poll reaps it
    finally:
        holder.rollback()
        holder.close()
    assert not c.in_transaction, "a failed reap left the connection inside a transaction"
    assert jobs.reap_stale_jobs(c) == 1
    c.close()
