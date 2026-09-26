"""Background schedule for a running server: index new photos and back up, unattended.

`due()` is pure (a clock and the library state in, a list of actions out) so the rules
are tested without threads. `run()` is the loop the server starts; it waits a full
minute before the first check, so starting the app never kicks off work by itself.
"""
from __future__ import annotations

import logging
import threading
import time

from . import db

log = logging.getLogger(__name__)
TICK_SECONDS = 60
BACKUP_RETRY_S = 6 * 3600      # after a backup that failed or was cancelled (drive unplugged…)


def due(settings, now: float, last_index: float, last_backup: float | None, busy: bool,
        backup_running: bool, last_backup_attempt: float | None = None) -> list[str]:
    actions = []
    if settings.auto_index_minutes > 0 and not busy and now - last_index >= settings.auto_index_minutes * 60:
        actions.append("index")
    if (settings.backup_folder and settings.backup_every_days > 0 and not backup_running
            and (last_backup is None or now - last_backup >= settings.backup_every_days * 86400)
            and (last_backup_attempt is None or now - last_backup_attempt >= BACKUP_RETRY_S
                 or (last_backup is not None and last_backup >= last_backup_attempt))):
        actions.append("backup")
    return actions


def start_backup(ctx, conn, target: str) -> int:
    from .engine import backup
    from .pipeline.jobs import create_job

    job_id = create_job(conn, "backup", {"folder": target})
    threading.Thread(target=_quiet, args=(backup.run_job, ctx, job_id, target), daemon=True,
                     name=f"backup-{job_id}").start()
    return job_id


def _quiet(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:
        pass            # recorded on the job


def run(ctx, stop: threading.Event) -> None:
    from .engine.backup import last_backup
    from .pipeline import jobs

    started = time.time()
    while not stop.wait(TICK_SECONDS):
        try:
            conn = ctx.connect()
            try:
                last_index = float(db.get_meta(conn, "last_auto_index") or started)
                lb = last_backup(conn)
                active = jobs.active_job(conn, exclude_kinds=jobs.LONG_SIDE_JOBS)
                attempt = db.get_meta(conn, "last_backup_attempt")
                backup_running = conn.execute("SELECT 1 FROM jobs WHERE kind = 'backup' AND status IN "
                                              "('running', 'queued')").fetchone() is not None
                now = time.time()
                for action in due(ctx.settings, now, last_index, lb["finished_at"] if lb else None,
                                  active is not None, backup_running,
                                  float(attempt) if attempt else None):
                    if action == "index":
                        db.set_meta(conn, "last_auto_index", now)
                        conn.commit()
                        jobs.spawn_index_job(ctx, {"kind": "index"})
                        log.info("Scheduled index started")
                    elif action == "backup":
                        db.set_meta(conn, "last_backup_attempt", now)
                        conn.commit()
                        start_backup(ctx, conn, ctx.settings.backup_folder)
                        log.info("Scheduled backup started to %s", ctx.settings.backup_folder)
            finally:
                conn.close()
        except Exception:
            log.exception("Scheduler tick failed")

