"""An unattended index run must be cheap when nothing changed.

The scheduler starts an index every hour (and one a minute after the server starts, if one is
overdue). Each ran all 17 post-processing stages whether or not the scan found anything, which on
a real 29,500-photo library meant about 11 minutes of disk work re-deriving results that could not
have changed, and long stretches holding the database write lock: the scheduler's own check failed
with "database is locked" while it ran, and so would an edit made in the web app.

So a *scheduled* run skips post-processing when the scan found nothing and no photo needed
analysis. A manual run never does, and a run that was cut off partway is not trusted.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import pytest

from photointel import db, scheduler
from photointel.pipeline import jobs
from photointel.pipeline import post as post_mod
from tests.conftest import make_image


@pytest.fixture
def post_calls(monkeypatch):
    """Counts how often post-processing really runs, while still running it."""
    calls: list[list[str] | None] = []
    real = post_mod.run_post_stages

    def spy(ctx, conn, stages=None, **kw):
        calls.append(stages)
        return real(ctx, conn, stages=stages, **kw)

    monkeypatch.setattr(post_mod, "run_post_stages", spy)
    return calls


def run(ctx, library, **kw):
    return jobs.run_index_job(ctx, roots=[str(library)], workers=2, **kw)


def meta(ctx, key):
    conn = ctx.connect()
    try:
        return db.get_meta(conn, key)
    finally:
        conn.close()


def test_a_scheduled_run_skips_post_processing_when_nothing_changed(ctx, library, post_calls):
    run(ctx, library)                                   # the first, full run
    assert len(post_calls) == 1
    out = run(ctx, library, only_if_changed=True)
    assert out["post"] == {"skipped": "nothing changed"}
    assert len(post_calls) == 1, "post-processing ran again although nothing had changed"


def test_a_manual_run_always_does_everything(ctx, library, post_calls):
    run(ctx, library)
    run(ctx, library)                                   # same library, flag off
    assert len(post_calls) == 2


def test_a_new_photo_brings_post_processing_back(ctx, library, post_calls):
    run(ctx, library)
    make_image(library / "DCIM/Camera/IMG_brand_new.jpg", colour=(10, 200, 30),
               taken=datetime(2024, 8, 1, 9, 0), noise=15)
    out = run(ctx, library, only_if_changed=True)
    assert "skipped" not in out["post"]
    assert len(post_calls) == 2


def test_a_changed_photo_brings_it_back(ctx, library, post_calls):
    run(ctx, library)
    target = library / "Trips/Goa/IMG_x0.jpg"
    make_image(target, colour=(250, 10, 10), taken=datetime(2024, 7, 20, 9, 5), noise=20)
    run(ctx, library, only_if_changed=True)
    assert len(post_calls) == 2


def test_a_removed_photo_brings_it_back(ctx, library, post_calls):
    run(ctx, library)
    (library / "Trips/Goa/IMG_x1.jpg").unlink()
    run(ctx, library, only_if_changed=True)
    assert len(post_calls) == 2


def test_a_run_cut_off_during_post_processing_is_not_trusted(ctx, library, post_calls):
    """If the process died mid post-processing, people/events/duplicates may be half-built. The
    next scheduled run must finish the job even though the scan shows nothing new."""
    run(ctx, library)
    conn = ctx.connect()
    db.set_meta(conn, "post_pending", "1")             # what a killed run leaves behind
    conn.commit()
    conn.close()
    out = run(ctx, library, only_if_changed=True)
    assert "skipped" not in out["post"]
    assert len(post_calls) == 2
    assert meta(ctx, "post_pending") == "0"            # and it is cleared once one completes
    out = run(ctx, library, only_if_changed=True)      # now it can skip again
    assert out["post"] == {"skipped": "nothing changed"}


def test_the_flag_is_set_while_post_processing_runs_and_cleared_after(ctx, library, monkeypatch):
    seen = {}
    real = post_mod.run_post_stages

    def watch(ctx_, conn, stages=None, **kw):
        seen["during"] = db.get_meta(ctx_.connect(), "post_pending")
        return real(ctx_, conn, stages=stages, **kw)

    monkeypatch.setattr(post_mod, "run_post_stages", watch)
    run(ctx, library)
    assert seen["during"] == "1"
    assert meta(ctx, "post_pending") == "0"


def test_an_explicit_post_only_run_is_never_skipped(ctx, library, post_calls):
    run(ctx, library)
    run(ctx, library, post_only=True, only_if_changed=True)
    assert len(post_calls) == 2


def test_a_partial_stage_run_does_not_touch_the_flag(ctx, library):
    """`--post-only --stages gpx,...` runs a few stages; that is not a finished full pass."""
    run(ctx, library)
    conn = ctx.connect()
    db.set_meta(conn, "post_pending", "1")
    conn.commit()
    conn.close()
    run(ctx, library, post_only=True, post_stages=["live-photos"])
    assert meta(ctx, "post_pending") == "1"


# ----------------------------------------------------------------- plumbing

def test_a_scheduled_job_is_launched_with_if_changed(ctx, _no_detached_jobs):
    jobs.spawn_index_job(ctx, {"kind": "index", "scheduled": True})
    assert "--if-changed" in _no_detached_jobs[-1]


def test_a_manual_job_is_not(ctx, _no_detached_jobs):
    jobs.spawn_index_job(ctx, {"kind": "index"})
    assert "--if-changed" not in _no_detached_jobs[-1]


def test_the_scheduler_asks_for_a_scheduled_run(ctx, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(jobs, "spawn_index_job", lambda c, params: captured.append(params) or 1)
    monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.05)
    ctx.settings.auto_index_minutes = 1
    conn = ctx.connect()
    db.set_meta(conn, "last_auto_index", 0)             # long overdue
    conn.commit()
    conn.close()

    stop = threading.Event()
    t = threading.Thread(target=scheduler.run, args=(ctx, stop), daemon=True)
    t.start()
    deadline = time.time() + 10
    want = {"kind": "index", "scheduled": True}
    # Wait for *the scheduled* request: an upload-folder index can come first on a slow machine (it failed CI
    # once on Windows by asserting on captured[0]), and that order is not what this test is about.
    while want not in captured and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    t.join(timeout=5)
    assert captured, "the scheduler never asked for an index"
    assert want in captured, f"no scheduled run among {captured}"


def test_the_command_line_flag_reaches_the_runner(ctx, library, capsys):
    """End to end through `python -m photointel index --if-changed` (the child process's argv)."""
    from photointel import cli

    run(ctx, library)                                   # index everything first, with the fake engines
    capsys.readouterr()
    cli.main(["--data", str(ctx.paths.data), "index", "--if-changed", "--no-faces", "--no-semantic",
              str(library)])
    printed = json.loads(capsys.readouterr().out)
    assert printed["post"] == {"skipped": "nothing changed"}
