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


UPLOAD_SETTLE_S = 60           # index uploads from backup apps once they pause for a minute


def uploads_due(pending_since: float | None, now: float, busy: bool) -> bool:
    return pending_since is not None and not busy and now - pending_since >= UPLOAD_SETTLE_S


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


HEALTH_EVERY_S = 15 * 60
RENEW_CHECK_S = 86400


def start_offsite(ctx, conn) -> int:
    from .engine import offsite
    from .pipeline.jobs import create_job

    job_id = create_job(conn, "offsite", {})
    threading.Thread(target=_quiet, args=(offsite.run_job, ctx, job_id), daemon=True,
                     name=f"offsite-{job_id}").start()
    return job_id


def _upkeep(ctx, conn, now: float, busy: bool, backup_running: bool) -> None:
    """The always-on chores: stay awake if asked, the off-site copy, health alerts, HTTPS renewal.
    Each is guarded on its own so one failing never stops the others."""
    from . import health, service, tls
    from .engine import offsite

    try:
        service.keep_awake(bool(ctx.settings.keep_awake))
    except Exception:
        log.debug("keep-awake failed", exc_info=True)
    try:
        lo = offsite.last(conn)
        attempt = db.get_meta(conn, "offsite_attempt")
        running = conn.execute("SELECT 1 FROM jobs WHERE kind = 'offsite' AND status IN ('running', 'queued')"
                               ).fetchone() is not None
        if not backup_running and offsite.due(ctx.settings, now, lo["finished_at"] if lo else None,
                                              float(attempt) if attempt else None, running):
            db.set_meta(conn, "offsite_attempt", now)
            conn.commit()
            start_offsite(ctx, conn)
            log.info("Scheduled off-site copy started")
    except Exception:
        log.exception("Off-site scheduling failed")
    try:
        last = float(db.get_meta(conn, "last_health_check") or 0)
        if now - last >= HEALTH_EVERY_S:
            db.set_meta(conn, "last_health_check", now)
            conn.commit()
            problems = health.check(ctx, conn, now)
            db.set_meta(conn, "health_problem_count", len(problems))
            conn.commit()
            health.notify(ctx, conn, problems, now)
    except Exception:
        log.exception("Health check failed")
    try:
        last = float(db.get_meta(conn, "last_tls_check") or 0)
        if ctx.settings.https_enabled and not busy and now - last >= RENEW_CHECK_S:
            db.set_meta(conn, "last_tls_check", now)
            conn.commit()
            if tls.renew_due(ctx.paths.data, now):
                files = tls.cert_files(ctx.paths.data)
                info = tls.cert_info(files[0]) if files else None
                if info and info["names"]:
                    tls.fetch_cert(ctx.paths.data, info["names"][0])
                    log.info("HTTPS certificate renewed")
                    if service.supervised():
                        service.request_restart(delay=5)      # the server loads a certificate only at start
    except Exception:
        log.exception("HTTPS renewal failed")


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
                _upkeep(ctx, conn, now, busy=active is not None, backup_running=backup_running)
                pending = db.get_meta(conn, "uploads_pending_since")
                if uploads_due(float(pending) if pending else None, now, active is not None):
                    from .engine.uploads import ensure_upload_root

                    db.set_meta(conn, "uploads_pending_since", "")
                    conn.commit()
                    _, root = ensure_upload_root(ctx, conn)
                    jobs.spawn_index_job(ctx, {"kind": "index", "roots": [str(root)]})
                    log.info("Indexing uploads from backup apps")
            finally:
                conn.close()
        except Exception:
            log.exception("Scheduler tick failed")

