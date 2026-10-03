"""A post-processing stage must not leave the job stalled behind its own open transaction.

In a job, the stages run on one connection and the progress reports go through another. When a
stage ended with an uncommitted write, the report that opens the next stage waited for that lock,
which the very thread making the report was holding, until SQLite's 60 s busy timeout gave up. On
a real library four stages that did almost no work (0.1-0.2 s on their own) each took 100-260 s
inside a job, so a scheduled index ran for 7-15 minutes every hour with the database write-locked
for much of it. The scheduler's own check failed with "database is locked", and so would an edit in
the web app. The original culprit was `import_takeout`, whose last statement wrote after its final
commit.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from photointel import db
from photointel.engine import places as places_mod
from photointel.engine import takeout as takeout_mod
from photointel.pipeline import jobs
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages


@pytest.fixture
def indexed(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    return ctx, library


def leaky(conn_holder):
    """A stage that writes and "forgets" to commit, as four real ones did."""
    def stage(ctx, conn, *a, **kw):
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('leak_probe', 'written')")
        conn_holder.append(conn)
        return {}
    return stage


def test_the_connection_is_idle_whenever_a_progress_report_is_made(indexed, monkeypatch):
    ctx, _ = indexed
    conn = ctx.connect()
    monkeypatch.setattr(places_mod, "geocode_photos", leaky([]))
    states: list[tuple[str, bool]] = []

    def progress(stage, i, total, msg=""):
        states.append((stage, conn.in_transaction))

    run_post_stages(ctx, conn, stages=["geocode", "uploads", "live-photos"], progress=progress)
    leaks = [s for s, open_ in states if open_]
    assert not leaks, f"a progress report was made while this connection held a write lock: {leaks}"
    assert len(states) >= 4


def test_a_stage_that_forgets_to_commit_cannot_stall_the_next_report(indexed, monkeypatch):
    """The real failure, timed: with a live reporter on its own connection."""
    ctx, _ = indexed
    conn = ctx.connect()
    job_id = jobs.create_job(conn, "index", {})
    conn.commit()
    reporter = jobs.JobReporter(ctx, job_id)
    reporter.start()
    monkeypatch.setattr(places_mod, "geocode_photos", leaky([]))
    t0 = time.time()
    run_post_stages(ctx, conn, stages=["geocode", "uploads", "live-photos"], progress=reporter.progress)
    took = time.time() - t0
    reporter.finish("done", "x")
    assert took < 3.0, f"the stage transitions took {took:.1f}s: a report waited on its own caller's lock"


def test_a_stages_writes_are_saved(indexed, monkeypatch):
    ctx, _ = indexed
    conn = ctx.connect()
    monkeypatch.setattr(places_mod, "geocode_photos", leaky([]))
    run_post_stages(ctx, conn, stages=["geocode"])
    other = ctx.connect()
    assert db.get_meta(other, "leak_probe") == "written"        # committed, so another connection sees it


def test_a_failed_stage_rolls_back_and_the_run_carries_on(indexed, monkeypatch):
    """Half-done writes from a stage that raised must not be swept into the next stage's commit."""
    ctx, _ = indexed
    conn = ctx.connect()

    def broken(ctx_, conn_, *a, **kw):
        conn_.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('half_done', '1')")
        raise RuntimeError("boom")

    monkeypatch.setattr(places_mod, "geocode_photos", broken)
    out = run_post_stages(ctx, conn, stages=["geocode", "uploads"])
    assert out["geocode"] == {"error": "boom"}
    assert "uploads" in out, "a failing stage must not stop the ones after it"
    assert db.get_meta(ctx.connect(), "half_done") is None


def test_takeout_leaves_nothing_uncommitted(indexed):
    """The original culprit: bump_generation ran after the final commit."""
    ctx, library = indexed
    photo = library / "Trips/Goa/IMG_x0.jpg"
    sidecar = photo.with_name(photo.name + ".supplemental-metadata.json")
    sidecar.write_text(json.dumps({
        "title": photo.name, "description": "a sidecar worth importing",
        "photoTakenTime": {"timestamp": str(int(datetime(2024, 7, 20, 9, 0, tzinfo=timezone.utc).timestamp()))},
    }), encoding="utf-8")
    conn = ctx.connect()
    out = takeout_mod.import_takeout(ctx, conn)
    assert out["sidecars"] >= 1, "the fixture sidecar was not picked up, so this test proves nothing"
    assert not conn.in_transaction


def test_the_progress_connection_gives_up_quickly(indexed):
    ctx, _ = indexed
    conn = ctx.connect()
    job_id = jobs.create_job(conn, "index", {})
    conn.commit()
    reporter = jobs.JobReporter(ctx, job_id)
    try:
        assert reporter.conn.execute("PRAGMA busy_timeout").fetchone()[0] <= 10_000
    finally:
        reporter.finish("done", "x")
