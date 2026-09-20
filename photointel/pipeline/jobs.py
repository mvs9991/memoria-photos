"""Background job bookkeeping (progress, cancellation, crash recovery)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
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


def reap_stale_jobs(conn) -> int:
    """Mark jobs whose process died as interrupted (their work is resumable)."""
    cutoff = time.time() - STALE_AFTER
    n = conn.execute(
        "UPDATE jobs SET status='interrupted', finished_at=?, message=COALESCE(message,'') || ' (process ended)' "
        "WHERE status IN ('running','queued') AND COALESCE(heartbeat_at, created_at) < ?",
        (time.time(), cutoff)).rowcount
    conn.commit()
    return n


def active_job(conn) -> dict | None:
    reap_stale_jobs(conn)
    row = conn.execute(
        "SELECT * FROM jobs WHERE status IN ('running','queued') ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


class JobReporter:
    """Writes progress/heartbeat for a job from its own connection."""

    def __init__(self, ctx, job_id: int | None):
        self.job_id = job_id
        self.ctx = ctx
        self.conn = ctx.connect() if job_id else None
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
                  post_stages: list[str] | None = None) -> dict:
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
        stats["post"] = run_post_stages(ctx, conn, stages=post_stages, progress=reporter.progress,
                                        full_recluster=full_recluster)
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


def spawn_index_job(ctx, params: dict) -> int:
    """Start indexing in a separate process so the web server stays responsive."""
    conn = ctx.connect()
    existing = active_job(conn)
    if existing:
        conn.close()
        return int(existing["id"])
    job_id = create_job(conn, params.get("kind", "index"), params)
    conn.close()
    if params.get("kind") == "caption":
        args = [sys.executable, "-m", "photointel", "--data", str(ctx.paths.data), "caption",
                "--limit", str(int(params.get("limit", 200)))]
        if params.get("detailed"):
            args.append("--detailed")
        _spawn(args)
        conn = ctx.connect()
        conn.execute("UPDATE jobs SET status='running', started_at=?, heartbeat_at=? WHERE id=?",
                     (time.time(), time.time(), job_id))
        conn.commit()
        conn.close()
        return job_id
    args = [sys.executable, "-m", "photointel", "--data", str(ctx.paths.data), "index", "--job-id", str(job_id)]
    if params.get("retry_errors"):
        args.append("--retry-errors")
    if params.get("post_only"):
        args.append("--post-only")
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
