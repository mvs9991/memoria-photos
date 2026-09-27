"""The off-site copy: an encrypted backup kept somewhere other than this house.

The local backup (backup.py) protects against a failed disk; it does not survive a fire, a
flood or a burglary that takes both drives. This keeps a second copy away from home, encrypted
before it leaves the computer, using restic (https://restic.net — open source, widely used,
and able to restore without Memoria). Where "away" is, is the user's choice:
  * a USB drive that lives at work or with family and comes home now and then (a folder path),
  * a relative's computer over SSH (`sftp:user@host:/path`), or their restic rest-server
    (`rest:http://host:8000/`),
  * or, if they choose, a cloud bucket restic supports (`s3:`, `b2:` …) — encrypted, so the
    provider sees only noise. Never the default.

Rules, like the local backup: copy-only. Snapshots are never removed (no `forget`/`prune`), so
a photo deleted here is still in every earlier snapshot. A restore goes into an empty folder
outside every photo folder, never over the library. The password is kept in <data>/offsite/ so
backups run unattended, and must also be written down: the copy is useless without it, and the
house that burned held this computer too.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .. import db
from .backup import snapshot_memoria
from .trash import TRASH_DIR_NAME
from .xmp import ExportError, refuse_inside_roots

log = logging.getLogger(__name__)
REMOTE = re.compile(r"^(sftp|rest|s3|b2|azure|gs|swift|rclone):", re.I)
TAG = "memoria"
ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/l/I
LATE_FACTOR = 1.5
CHECK_EVERY_S = 30 * 86400


class OffsiteError(RuntimeError):
    pass


def folder(ctx) -> Path:
    return ctx.paths.data / "offsite"


def password_file(ctx) -> Path:
    return folder(ctx) / "restic-password"


def new_password() -> str:
    """20 unambiguous characters (≈115 bits) in groups of five, to write down."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(20))
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def find_restic(ctx) -> Path | None:
    exe = "restic.exe" if os.name == "nt" else "restic"
    # winget's links folder too: a server that was running when restic was installed has the old PATH.
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / exe if os.name == "nt" else None
    for c in (ctx.settings.restic_path, os.environ.get("PHOTOINTEL_RESTIC"), shutil.which("restic"),
              str(ctx.paths.data / "bin" / exe), str(winget) if winget else None):
        if c and Path(c).is_file():
            return Path(c)
    return None


def _cmd(exe: Path) -> list[str]:
    return [sys.executable, str(exe)] if exe.suffix.lower() == ".py" else [str(exe)]      # the tests' stand-in


def _env(ctx, repo: str | None = None) -> dict:
    env = dict(os.environ)
    env.update({"RESTIC_REPOSITORY": repo or ctx.settings.offsite_repo,
                "RESTIC_PASSWORD_FILE": str(password_file(ctx)),
                "RESTIC_CACHE_DIR": str(ctx.paths.data / "cache" / "restic"),
                "RESTIC_PROGRESS_FPS": "0.5"})
    env.pop("RESTIC_PASSWORD", None)
    env.pop("RESTIC_PASSWORD_COMMAND", None)
    return env


def _restic(ctx, *args: str, repo: str | None = None, timeout: int | None = 600) -> subprocess.CompletedProcess:
    exe = find_restic(ctx)
    if exe is None:
        raise OffsiteError("restic is not installed (see Settings > Off-site copy)")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0          # type: ignore[attr-defined]
    return subprocess.run([*_cmd(exe), *args], env=_env(ctx, repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)


def _fail_text(r: subprocess.CompletedProcess) -> str:
    text = (r.stderr or r.stdout or "").strip().splitlines()
    return " ".join(text[-3:])[:400] or f"restic exited with {r.returncode}"


def check_location(ctx, conn: sqlite3.Connection, repo: str) -> str:
    """A folder must be outside every photo folder and Memoria's data (it would copy itself)."""
    repo = repo.strip()
    if not repo:
        raise ExportError("choose where the off-site copy goes")
    if REMOTE.match(repo):
        return repo
    p = Path(repo).expanduser().resolve()
    refuse_inside_roots(conn, [p])
    data = ctx.paths.data.resolve()
    if p == data or data in p.parents:
        raise ExportError("choose a folder outside Memoria's data folder")
    return str(p)


def setup(ctx, conn: sqlite3.Connection, repo: str, password: str) -> dict:
    """Create the encrypted store (or open an existing one with this password) and remember it."""
    repo = check_location(ctx, conn, repo)
    if len(password) < 12:
        raise OffsiteError("use a password of at least 12 characters")
    pw = password_file(ctx)
    pw.parent.mkdir(parents=True, exist_ok=True)
    old = pw.read_text(encoding="utf-8") if pw.exists() else None
    pw.write_text(password, encoding="utf-8")
    try:
        if not REMOTE.match(repo):
            Path(repo).mkdir(parents=True, exist_ok=True)
        r = _restic(ctx, "cat", "config", repo=repo, timeout=120)
        created = False
        if r.returncode != 0:
            if "wrong password" in (r.stderr or "").lower() or r.returncode == 12:
                raise OffsiteError("a copy already exists there with a different password")
            r = _restic(ctx, "init", repo=repo, timeout=300)
            if r.returncode != 0:
                raise OffsiteError(_fail_text(r))
            created = True
    except Exception:
        if old is None:
            pw.write_text("", encoding="utf-8")
        else:
            pw.write_text(old, encoding="utf-8")
        raise
    ctx.settings.offsite_repo = repo
    ctx.settings.save(ctx.paths.data)
    db.audit(conn, "offsite_setup", "library", None, {"repo": repo, "created": created})
    conn.commit()
    return {"repo": repo, "created": created}


def reveal_password(ctx) -> str:
    try:
        return password_file(ctx).read_text(encoding="utf-8")
    except OSError:
        return ""


def _sources(ctx, conn: sqlite3.Connection) -> list[tuple[Path | None, str, str]]:
    """-> (folder to run restic in, what to name, the folder's full path) for each thing copied.

    Each photo folder is named relative to its parent, run from there. Given a full path,
    restic records every folder above it too (C:\\, C:\\Users…) with its Windows permissions,
    and a restore recreates them: found here, restoring C\\Users locked the restored folder
    against its own user. Named relatively, the copy holds the photo folder and nothing above.
    A whole drive (E:\\) has no name and is given in full."""
    out = []
    for (path,) in conn.execute("SELECT path FROM roots ORDER BY id"):
        p = Path(path)
        if not p.is_dir():
            continue
        out.append((p.parent, p.name, str(p)) if p.name else (None, str(p), str(p)))
    staging = folder(ctx) / "staging"
    out.append((staging, "memoria", str(staging / "memoria")))
    return out


def _backup_one(ctx, exe: Path, cwd: Path | None, name: str, progress, should_stop) -> dict | None:
    """One restic snapshot of one folder. None when cancelled."""
    args = [*_cmd(exe), "backup", "--json", "--tag", TAG, "--host", "memoria",
            "--exclude", TRASH_DIR_NAME, "--exclude", ".incoming", name]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0          # type: ignore[attr-defined]
    # One stream: reading only stdout while errors pile up unread on stderr could stall restic.
    proc = subprocess.Popen(args, cwd=str(cwd) if cwd else None, env=_env(ctx), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            creationflags=flags)
    summary: dict = {}
    errors: list[str] = []
    plain: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        if should_stop and should_stop():
            proc.terminate()
            proc.wait(timeout=60)
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            if line.strip():
                plain = (plain + [line.strip()])[-5:]
            continue
        kind = msg.get("message_type")
        if kind == "status" and progress:
            done, total = msg.get("files_done", 0), msg.get("total_files", 0)
            progress(float(msg.get("percent_done", 0)), f"{done:,} of {total:,} files checked" if total else "Starting")
        elif kind == "error":
            err = msg.get("error") or {}
            errors.append(f"{msg.get('item', '')}: {err.get('message', err) if isinstance(err, dict) else err}")
        elif kind == "summary":
            summary = msg
    try:
        code = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = -1
    # 0 = done; 3 = the snapshot was made but some files could not be read.
    if code not in (0, 3) or not summary.get("snapshot_id"):
        raise OffsiteError(" ".join(plain[-3:])[:400] or f"restic exited with {code}")
    summary["unreadable"] = errors
    return summary


def run(ctx, conn: sqlite3.Connection, progress: Callable[[float, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> dict:
    """A snapshot of every reachable photo folder and of Memoria's own data."""
    if not ctx.settings.offsite_repo:
        raise OffsiteError("no off-site copy is set up")
    exe = find_restic(ctx)
    if exe is None:
        raise OffsiteError("restic is not installed")
    t0 = time.time()
    snapshot_memoria(ctx, conn, folder(ctx) / "staging" / "memoria")
    sources = _sources(ctx, conn)
    totals = {"files_new": 0, "files_changed": 0, "files": 0, "bytes_added": 0}
    unreadable: list[str] = []
    snaps = []
    for i, (cwd, name, full) in enumerate(sources):
        step = (lambda f, msg, i=i: progress((i + f) / len(sources), f"{Path(full).name or full}: {msg}")) \
            if progress else None
        s = _backup_one(ctx, exe, cwd, name, step, should_stop)
        if s is None:
            return {"cancelled": True}
        snaps.append(s["snapshot_id"])
        totals["files_new"] += s.get("files_new", 0)
        totals["files_changed"] += s.get("files_changed", 0)
        totals["files"] += s.get("total_files_processed", 0)
        totals["bytes_added"] += s.get("data_added", 0)
        unreadable += s["unreadable"]
    out = {"finished_at": time.time(), "seconds": round(time.time() - t0, 1), "snapshots": snaps,
           "unreadable": unreadable[:20], "folders": len(sources) - 1, **totals}
    db.set_meta(conn, "offsite_last", json.dumps(out))
    conn.commit()
    return out


def last(conn: sqlite3.Connection, key: str = "offsite_last") -> dict | None:
    try:
        raw = db.get_meta(conn, key)
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def snapshots(ctx) -> list[dict]:
    """Newest first: id, time and the folder each snapshot holds."""
    r = _restic(ctx, "snapshots", "--json", "--tag", TAG, timeout=300)
    if r.returncode != 0:
        raise OffsiteError(_fail_text(r))
    out = [{"id": s.get("short_id") or s.get("id", "")[:8], "full_id": s.get("id", ""), "time": s.get("time", ""),
            "paths": s.get("paths", [])} for s in json.loads(r.stdout or "[]")]
    return sorted(out, key=lambda s: s["time"], reverse=True)


def verify(ctx, conn: sqlite3.Connection, subset: str = "5%") -> dict:
    """Read back a sample of the stored data and check it decrypts and matches."""
    r = _restic(ctx, "check", f"--read-data-subset={subset}", timeout=None)
    out = {"at": time.time(), "ok": r.returncode == 0, "detail": "" if r.returncode == 0 else _fail_text(r),
           "subset": subset}
    db.set_meta(conn, "offsite_check", json.dumps(out))
    conn.commit()
    return out


def restore_snapshot(ctx, conn: sqlite3.Connection, target: str, snapshot: str = "latest") -> dict:
    """Into an empty folder outside the library. "latest" brings back the newest copy of every
    folder (each photo folder, and `memoria` with the database), each under its own name."""
    if not re.fullmatch(r"latest|[0-9a-f]{8,64}", snapshot):
        raise OffsiteError("not a snapshot id")
    t = Path(target).expanduser().resolve()
    refuse_inside_roots(conn, [t])
    data = ctx.paths.data.resolve()
    if t == data or data in t.parents:
        raise ExportError("choose a folder outside Memoria's data folder")
    if t.exists() and any(t.iterdir()):
        raise ExportError("restore into an empty folder")
    if snapshot == "latest":
        newest: dict[tuple, dict] = {}
        for s in snapshots(ctx):
            newest.setdefault(tuple(s["paths"]), s)             # newest first, so the first seen wins
        chosen = list(newest.values())
    else:
        chosen = [s for s in snapshots(ctx) if s["full_id"].startswith(snapshot) or s["id"] == snapshot]
    if not chosen:
        raise OffsiteError("no such snapshot")
    t.mkdir(parents=True, exist_ok=True)
    names: set[str] = set()
    restored, warnings = [], []
    for s in chosen:
        base = Path(s["paths"][0]).name if len(s["paths"]) == 1 else ""
        into = t
        if base.lower() in names:                               # two photo folders with one name
            into = t / f"{base} ({len(names) + 1})"
            into.mkdir(parents=True, exist_ok=True)
        names.add(base.lower())
        # --verify reads every restored file back against the snapshot.
        r = _restic(ctx, "restore", s["full_id"] or s["id"], "--target", str(into), "--verify", timeout=None)
        harmless = _harmless_restore_errors(r)
        if r.returncode != 0 and harmless is None:
            raise OffsiteError(_fail_text(r))
        warnings += harmless or []
        restored.append({"snapshot": s["id"], "paths": s["paths"], "into": str(into)})
    db.audit(conn, "offsite_restore", "library", None, {"snapshot": snapshot, "target": str(t)})
    conn.commit()
    return {"target": str(t), "restored": restored, "warnings": warnings}


def _harmless_restore_errors(r: subprocess.CompletedProcess) -> list[str] | None:
    """restic exits 1 when it could not set a folder's date (seen on Windows) although every
    file was restored and verified. Only such errors are warnings; anything else is a failure."""
    if r.returncode != 1:
        return None
    lines = [ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.lower().startswith("ignoring error")]
    rest = [ln for ln in lines if not ln.lower().startswith(("ignoring error", "fatal: there were"))]
    if errors and all("timestamp" in e.lower() for e in errors) and not any("error" in ln.lower() for ln in rest):
        return errors
    return None


def due(settings, now: float, last_ok: float | None, attempt: float | None, running: bool) -> bool:
    from ..scheduler import BACKUP_RETRY_S

    if not settings.offsite_repo or settings.offsite_every_days <= 0 or running:
        return False
    if last_ok is not None and now - last_ok < settings.offsite_every_days * 86400:
        return False
    return attempt is None or now - attempt >= BACKUP_RETRY_S or (last_ok is not None and last_ok >= attempt)


def check_due(settings, now: float, last_check: dict | None, last_ok: dict | None) -> bool:
    return bool(settings.offsite_repo and last_ok and (last_check is None or now - last_check["at"] >= CHECK_EVERY_S))


def problems(ctx, conn: sqlite3.Connection, now: float) -> list[dict]:
    from ..health import _day, _p

    s = ctx.settings
    if not s.offsite_repo:
        return [_p("offsite:none", "warn", "No copy outside this house",
                   "A fire, flood or theft could take this computer and its backup drive together.",
                   "Settings > Off-site copy: an encrypted copy on a drive kept elsewhere, or a relative's computer.")]
    out = []
    if find_restic(ctx) is None:
        out.append(_p("offsite:restic", "error", "The off-site copy can't run",
                      "restic, the program that makes it, is not installed here.", "Settings > Off-site copy."))
    lo = last(conn)
    every = max(s.offsite_every_days, 1)
    if lo is None:
        out.append(_p("offsite:never", "warn", "The off-site copy has not been made yet",
                      "It is set up, but no copy has finished.", "Settings > Off-site copy > Copy now."))
    elif now - lo["finished_at"] > every * 86400 * LATE_FACTOR:
        out.append(_p("offsite:late", "warn", "The off-site copy is out of date",
                      f"The last one finished on {_day(lo['finished_at'])}.",
                      "If it goes to a drive kept elsewhere, bring it home and plug it in."))
    job = conn.execute("SELECT status, error, message, finished_at FROM jobs WHERE kind = 'offsite' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if job and job["status"] == "failed" and (lo is None or (job["finished_at"] or now) > lo["finished_at"]):
        out.append(_p("offsite:failed", "error", "The last off-site copy failed",
                      (job["error"] or job["message"] or "no reason recorded")[:300], "Settings > Off-site copy."))
    chk = last(conn, "offsite_check")
    if chk and not chk["ok"]:
        out.append(_p("offsite:check", "error", "The off-site copy failed its check",
                      chk["detail"][:300] or "Part of it could not be read back.",
                      "Make a new copy to a fresh location; keep the old one until it succeeds."))
    return out


def run_job(ctx, job_id: int | None) -> dict:
    from ..pipeline.jobs import JobReporter

    reporter = JobReporter(ctx, job_id)
    reporter.start()
    conn = ctx.connect()

    def should_stop() -> bool:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id else None
        return bool(row and row[0])

    try:
        out = run(ctx, conn, should_stop=should_stop,
                  progress=lambda f, msg: reporter.progress("offsite", int(f * 1000), 1000, msg))
        if out.get("cancelled"):
            reporter.finish("cancelled", "Off-site copy cancelled")
            return out
        msg = f"Off-site copy made: {out['files_new']:,} new and {out['files_changed']:,} changed files"
        if out["unreadable"]:
            msg += f"; {len(out['unreadable'])} could not be read"
        if check_due(ctx.settings, time.time(), last(conn, "offsite_check"), out):
            reporter.progress("offsite", 1000, 1000, "Reading back a sample of the copy")
            if not verify(ctx, conn)["ok"]:
                msg += "; its check FAILED (see Settings)"
        reporter.finish("done", msg)
        return out
    except Exception as exc:
        log.exception("Off-site copy failed")
        reporter.finish("failed", "Off-site copy failed", str(exc))
        raise
    finally:
        conn.close()


def install_hint() -> str:
    if os.name == "nt":
        return "winget install restic.restic   (or download restic from https://restic.net)"
    if sys.platform == "darwin":
        return "brew install restic"
    return "sudo apt install restic   (or your system's package manager)"
