"""Background job bookkeeping (progress, cancellation, crash recovery)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import sqlite3
import time
from pathlib import Path

from .. import db

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 5.0
STALE_AFTER = 60.0


_LOCAL_LOCKS: set[str] = set()
_LOCAL_LOCKS_GUARD = threading.Lock()


class IndexLock:
    """Exclusive lock for a library's indexing pipeline, across processes and threads.

    Two concurrent runs would double the GPU load, interleave their progress
    reports and race on the same photo rows. The lock is a file holding the owner
    pid; a lock whose process is gone (a hard kill, a power cut) is taken over.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.acquired = False

    def _owner_alive(self) -> int | None:
        try:
            pid = int(self.path.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return None
        if pid == os.getpid():
            return None
        try:
            if os.name == "nt":
                import subprocess

                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                     capture_output=True, text=True, timeout=10).stdout
                return pid if str(pid) in out else None
            os.kill(pid, 0)
            return pid
        except Exception:
            return None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path.resolve())
        with _LOCAL_LOCKS_GUARD:
            if key in _LOCAL_LOCKS:
                log.warning("Indexing is already running in this process")
                return False
            _LOCAL_LOCKS.add(key)
        owner = self._owner_alive()
        if owner:
            log.warning("Another indexing run is already active (pid %s); not starting a second one", owner)
            with _LOCAL_LOCKS_GUARD:
                _LOCAL_LOCKS.discard(key)
            return False
        try:
            self.path.write_text(f"{os.getpid()} {time.time()}", encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write the index lock (%s); continuing without it", exc)
            self.acquired = True
            return True
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        with _LOCAL_LOCKS_GUARD:
            _LOCAL_LOCKS.discard(str(self.path.resolve()))
        try:
            if self.path.exists() and self.path.read_text(encoding="utf-8").startswith(str(os.getpid())):
                self.path.unlink()
        except OSError:
            pass
        self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def create_job(conn, kind: str, params: dict | None = None) -> int:
    cur = conn.execute("INSERT INTO jobs(kind, status, params, created_at) VALUES (?, 'queued', ?, ?)",
                       (kind, json.dumps(params or {}), time.time()))
    conn.commit()
    return int(cur.lastrowid)


# Jobs that only read the library and run alongside indexing (never "the" index job).
LONG_SIDE_JOBS = ("export", "backup", "create", "offsite")


def reap_stale_jobs(conn) -> int:
    """Mark jobs whose process died as interrupted (their work is resumable)."""
    cutoff = time.time() - STALE_AFTER
    # Read first: this runs on every jobs poll and scheduler tick, and an UPDATE takes the write lock
    # even when it matches nothing.
    stale = conn.execute("SELECT 1 FROM jobs WHERE status IN ('running','queued') "
                         "AND COALESCE(heartbeat_at, created_at) < ? LIMIT 1", (cutoff,)).fetchone()
    if stale is None:
        return 0
    try:
        n = conn.execute(
            "UPDATE jobs SET status='interrupted', finished_at=?, message=COALESCE(message,'') || ' (process ended)' "
            "WHERE status IN ('running','queued') AND COALESCE(heartbeat_at, created_at) < ?",
            (time.time(), cutoff)).rowcount
        conn.commit()
        return n
    except sqlite3.OperationalError:
        # Lost the race for the lock: tidying is best-effort and the next poll does it. Never leave the
        # half-opened transaction behind (see ApiState.conn).
        conn.rollback()
        return 0


def active_job(conn, exclude_kinds: tuple[str, ...] = ()) -> dict | None:
    reap_stale_jobs(conn)
    marks = ",".join("?" * len(exclude_kinds))
    row = conn.execute(
        "SELECT * FROM jobs WHERE status IN ('running','queued')"
        + (f" AND kind NOT IN ({marks})" if exclude_kinds else "") + " ORDER BY id DESC LIMIT 1",
        exclude_kinds).fetchone()
    return dict(row) if row else None


class JobReporter:
    """Writes progress/heartbeat for a job from its own connection."""

    def __init__(self, ctx, job_id: int | None):
        self.job_id = job_id
        self.ctx = ctx
        self.conn = ctx.connect() if job_id else None
        if self.conn is not None:
            # Progress and heartbeats are best-effort. With the default 60 s timeout, one write
            # that lost the race for the lock stalled whichever thread made it for a full minute.
            self.conn.execute("PRAGMA busy_timeout = 5000")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.job_id:
            return
        with self._lock:
            self.conn.execute("UPDATE jobs SET status='running', started_at=?, heartbeat_at=?, pid=? WHERE id=?",
                              (time.time(), time.time(), os.getpid(), self.job_id))
            self.conn.commit()
        self._thread = threading.Thread(target=self._beat, daemon=True, name="job-heartbeat")
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                with self._lock:
                    self.conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (time.time(), self.job_id))
                    self.conn.commit()
            except Exception:
                log.debug("heartbeat failed", exc_info=True)

    def progress(self, stage: str, done: int, total: int, message: str = "") -> None:
        if not self.job_id:
            if total:
                log.info("[%s] %d/%d %s", stage, done, total, message)
            return
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE jobs SET stage=?, progress_done=?, progress_total=?, message=?, heartbeat_at=? WHERE id=?",
                    (stage, done, total, message, time.time(), self.job_id))
                self.conn.commit()
        except Exception:
            log.debug("progress write failed", exc_info=True)

    def finish(self, status: str, message: str = "", error: str | None = None) -> None:
        self._stop.set()
        if not self.job_id:
            return
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE jobs SET status=?, finished_at=?, message=?, error=? WHERE id=?",
                    (status, time.time(), message, error, self.job_id))
                self.conn.commit()
                self.conn.close()
        except Exception:
            log.debug("finish write failed", exc_info=True)


def run_index_job(ctx, job_id: int | None = None, roots: list[str] | None = None, retry_errors: bool = False,
                  skip_faces: bool = False, skip_semantic: bool = False, post_only: bool = False,
                  workers: int | None = None, full_recluster: bool = False,
                  post_stages: list[str] | None = None, only_if_changed: bool = False) -> dict:
    """Index, then run the post-processing stages.

    `only_if_changed` is for unattended runs: when the scan found nothing new, changed, missing
    or restored and no photo needed analysis, the post-processing is skipped. Left alone, the
    hourly scheduled run spent about 11 minutes re-deriving results that could not have changed
    (and held the database write lock for much of it, so edits in the web app timed out).
    A run that was cut off partway through post-processing is not trusted: `post_pending` stays
    set until one finishes, and the next run does it all.
    """
    from .indexer import CancelledError, Indexer
    from .post import run_post_stages

    lock = IndexLock(ctx.paths.data / "index.lock")
    if not lock.acquire():
        msg = "Another indexing run is already in progress for this library"
        if job_id:
            c = ctx.connect()
            c.execute("UPDATE jobs SET status='cancelled', finished_at=?, message=? WHERE id=?",
                      (time.time(), msg, job_id))
            c.commit()
            c.close()
        return {"skipped": msg}

    reporter = JobReporter(ctx, job_id)
    reporter.start()
    conn = ctx.connect()
    stats: dict = {}
    try:
        if roots is None:
            roots = [r[0] for r in conn.execute("SELECT path FROM roots ORDER BY id")]
        if not roots and not post_only:
            log.warning("No library roots registered")
        if not post_only:
            indexer = Indexer(ctx, job_id=job_id, progress_cb=reporter.progress,
                              enable_faces=not skip_faces, enable_semantic=not skip_semantic, workers=workers)
            stats["index"] = indexer.run(roots=roots, retry_errors=retry_errors)
        if only_if_changed and not post_only and _nothing_changed(conn, stats.get("index", {})):
            stats["post"] = {"skipped": "nothing changed"}
            reporter.finish("done", "Nothing new")
            return stats
        full_post = post_stages is None
        if full_post:
            db.set_meta(conn, "post_pending", "1")
            conn.commit()
        stats["post"] = run_post_stages(ctx, conn, stages=post_stages, progress=reporter.progress,
                                        full_recluster=full_recluster)
        if full_post:
            db.set_meta(conn, "post_pending", "0")
            conn.commit()
        reporter.finish("done", "Finished")
    except CancelledError:
        log.warning("Job cancelled")
        stats["cancelled"] = True
        reporter.finish("cancelled", "Cancelled by user")
    except KeyboardInterrupt:
        stats["cancelled"] = True
        reporter.finish("cancelled", "Interrupted")
        raise
    except Exception as exc:
        log.exception("Job failed")
        reporter.finish("failed", "Failed", str(exc))
        raise
    finally:
        conn.close()
        lock.release()
    return stats


def _nothing_changed(conn, index_stats: dict) -> bool:
    """True when a run found no work AND the last post-processing is known to have finished."""
    scan = index_stats.get("scan")
    if scan is None:                       # no scan happened, so there is nothing to compare
        return False
    if any(scan.values()) or index_stats.get("total", 1) or index_stats.get("processed", 0):
        return False
    return db.get_meta(conn, "post_pending") != "1"


def run_caption_job(ctx, job_id: int | None = None, limit: int = 200, detailed: bool = False) -> dict:
    """Caption photos as a tracked job: heartbeat, progress and cancellation like indexing."""
    from ..vision.captioner import caption_photos

    reporter = JobReporter(ctx, job_id)
    reporter.start()
    conn = ctx.connect()

    def should_stop() -> bool:
        if not job_id:
            return False
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    try:
        out = caption_photos(ctx, conn, limit=limit, detailed=detailed, should_stop=should_stop,
                             progress=lambda d, t: reporter.progress("caption", d, t, f"Described {d:,} of {t:,}"))
        if out.get("cancelled"):
            reporter.finish("cancelled", "Cancelled by user")
        else:
            reporter.finish("done", f"Described {out.get('captioned', 0):,} photos")
        return out
    except Exception as exc:
        log.exception("Caption job failed")
        reporter.finish("failed", "Failed", str(exc))
        raise
    finally:
        conn.close()


def run_ocr_job(ctx, job_id: int | None = None, everything: bool = False, limit: int | None = None) -> dict:
    """Read text in photos as a tracked job (heartbeat, progress, cancellation)."""
    from ..engine.ocr import ocr_photos
    from .post import rebuild_fts

    reporter = JobReporter(ctx, job_id)
    reporter.start()
    conn = ctx.connect()

    def should_stop() -> bool:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id else None
        return bool(row and row[0])

    try:
        out = ocr_photos(ctx, conn, everything=everything, limit=limit, should_stop=should_stop,
                         progress=lambda d, t: reporter.progress("ocr", d, t, f"Read {d:,} of {t:,}"))
        rebuild_fts(conn)   # make the new text searchable straight away
        reporter.finish("cancelled" if should_stop() else "done", f"Read text in {out.get('read', 0):,} photos")
        return out
    except Exception as exc:
        log.exception("OCR job failed")
        reporter.finish("failed", "Failed", str(exc))
        raise
    finally:
        conn.close()


def spawn_index_job(ctx, params: dict) -> int:
    """Start indexing in a separate process so the web server stays responsive."""
    conn = ctx.connect()
    # An export runs alongside (it only reads the library); it must not swallow an index request.
    existing = active_job(conn, exclude_kinds=LONG_SIDE_JOBS)
    if existing:
        conn.close()
        return int(existing["id"])
    job_id = create_job(conn, params.get("kind", "index"), params)
    conn.close()
    if params.get("kind") == "ocr":
        args = [sys.executable, "-m", "photointel", "--data", str(ctx.paths.data), "ocr", "--job-id", str(job_id)]
        if params.get("all"):
            args.append("--all")
        _spawn(args)
        return job_id
    if params.get("kind") == "caption":
        # The job id lets the child process heartbeat and see cancellation; without it
        # the reaper marks a job that is still running as interrupted after a minute.
        args = [sys.executable, "-m", "photointel", "--data", str(ctx.paths.data), "caption",
                "--job-id", str(job_id), "--limit", str(int(params.get("limit", 200)))]
        if params.get("detailed"):
            args.append("--detailed")
        _spawn(args)
        return job_id
    args = [sys.executable, "-m", "photointel", "--data", str(ctx.paths.data), "index", "--job-id", str(job_id)]
    if params.get("retry_errors"):
        args.append("--retry-errors")
    if params.get("scheduled"):
        args.append("--if-changed")
    if params.get("post_only"):
        args.append("--post-only")
        if params.get("stages"):
            args += ["--stages", ",".join(params["stages"])]
    if params.get("skip_faces"):
        args.append("--no-faces")
    if params.get("skip_semantic"):
        args.append("--no-semantic")
    for r in params.get("roots", []) or []:
        args.append(r)
    log.info("Spawning job %d: %s", job_id, " ".join(args))
    _spawn(args)
    return job_id


def _spawn(args: list[str]) -> None:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    subprocess.Popen(args, cwd=str(Path(__file__).resolve().parent.parent.parent), creationflags=creationflags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def cancel_job(conn, job_id: int) -> None:
    conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
    conn.commit()
