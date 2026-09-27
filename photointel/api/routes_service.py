"""Always-on: health alerts, the keeper and auto-start, HTTPS, and the off-site copy. Owner only
(see accounts.OWNER_ONLY)."""
from __future__ import annotations

import os
import sys
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import accounts, autostart, health, service, tls
from ..engine import offsite
from ..engine.xmp import ExportError
from .deps import get_state

router = APIRouter()


# ---------------------------------------------------------------- alerts

@router.get("/alerts")
def alerts():
    state = get_state()
    conn = state.conn()
    problems = health.check(state.ctx, conn)
    shown, snoozed = health.visible(conn, problems)
    return {"problems": shown, "snoozed": snoozed, "webhook": bool(state.ctx.settings.alert_webhook)}


class SnoozeBody(BaseModel):
    key: str = Field(..., max_length=200)
    days: int = Field(7, ge=1, le=90)


@router.post("/alerts/snooze")
def snooze(body: SnoozeBody):
    health.snooze(get_state().conn(), body.key, body.days)
    return {"ok": True}


@router.post("/alerts/test")
def test_alert():
    s = get_state().ctx.settings
    if not s.alert_webhook:
        raise HTTPException(400, "set an alert address first")
    try:
        health._post(s.alert_webhook, "Memoria: test alert", "Alerts from your photo library will arrive like this.",
                     "warn")
    except Exception as exc:
        raise HTTPException(502, f"could not send: {exc}")
    return {"ok": True}


# ---------------------------------------------------------------- the keeper and auto-start

@router.get("/service")
def service_state():
    ctx = get_state().ctx
    st = service.read_state(ctx.paths.data)
    return {"supervised": service.supervised(),
            "keeper": {"status": st.get("status"), "started_at": st.get("keeper_started_at"),
                       "restarts_today": service.restarts_since(ctx.paths.data, time.time() - 86400)},
            "autostart": autostart.status(),
            "keep_awake_supported": os.name == "nt",
            "platform": sys.platform}


class OnBody(BaseModel):
    on: bool


@router.post("/service/autostart")
def set_autostart(body: OnBody):
    ctx = get_state().ctx
    try:
        if body.on:
            autostart.install(ctx.paths.data)
        else:
            autostart.remove()
    except (PermissionError, OSError) as exc:
        raise HTTPException(400, str(exc))
    return autostart.status()


@router.post("/service/restart")
def restart():
    if not service.supervised():
        raise HTTPException(409, "Memoria was started by hand: close it and start it again "
                                 "(or turn on auto-start, which restarts by itself)")
    service.request_restart()
    return {"restarting": True}


# ---------------------------------------------------------------- HTTPS through Tailscale

def _https_state(ctx) -> dict:
    files = tls.cert_files(ctx.paths.data)
    info = tls.cert_info(files[0]) if files else None
    return {"enabled": ctx.settings.https_enabled, "port": ctx.settings.https_port, "cert": info,
            "url": tls.https_url(ctx.settings, info) if ctx.settings.https_enabled else "",
            "supervised": service.supervised()}


@router.get("/https")
def https_state():
    state = get_state()
    ctx = state.ctx
    out = _https_state(ctx)
    out["tailscale"] = tls.tailscale_status()
    out["protected"] = bool(ctx.settings.access_password_hash) or accounts.enabled(state.conn())
    return out


@router.post("/https/setup")
def https_setup():
    state = get_state()
    ctx = state.ctx
    if not (ctx.settings.access_password_hash or accounts.enabled(state.conn())):
        raise HTTPException(400, "set a password (or turn on accounts) first: HTTPS makes Memoria reachable "
                                 "from your other devices")
    ts = tls.tailscale_status()
    if not ts.get("installed"):
        raise HTTPException(400, "Tailscale is not installed on this computer")
    if not ts.get("running"):
        raise HTTPException(400, "Tailscale is installed but not signed in")
    if not ts.get("https_ready"):
        raise HTTPException(400, "turn on HTTPS certificates for your tailnet first (Tailscale admin console → DNS)")
    try:
        tls.fetch_cert(ctx.paths.data, ts["domain"])
    except Exception as exc:
        raise HTTPException(502, str(exc))
    ctx.settings.https_enabled = True
    ctx.settings.save(ctx.paths.data)
    restarting = service.supervised()
    if restarting:
        service.request_restart()
    return {**_https_state(ctx), "restarting": restarting}


@router.post("/https/off")
def https_off():
    ctx = get_state().ctx
    ctx.settings.https_enabled = False
    ctx.settings.save(ctx.paths.data)
    restarting = service.supervised()
    if restarting:
        service.request_restart()
    return {**_https_state(ctx), "restarting": restarting}


# ---------------------------------------------------------------- the off-site copy

def _running(conn) -> dict | None:
    row = conn.execute("SELECT id, status, message, progress_done, progress_total FROM jobs WHERE kind = 'offsite' "
                       "AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


@router.get("/offsite")
def offsite_state():
    state = get_state()
    ctx, conn = state.ctx, state.conn()
    restic = offsite.find_restic(ctx)
    return {"repo": ctx.settings.offsite_repo, "every_days": ctx.settings.offsite_every_days,
            "restic": str(restic) if restic else "", "install_hint": offsite.install_hint(),
            "last": offsite.last(conn), "last_check": offsite.last(conn, "offsite_check"),
            "running": _running(conn), "checking": _checking.is_set()}


class OffsiteSetup(BaseModel):
    repo: str = Field(..., min_length=1, max_length=1000)
    password: str | None = Field(None, max_length=200)


@router.post("/offsite/setup")
def offsite_setup(body: OffsiteSetup):
    state = get_state()
    generated = not body.password
    password = offsite.new_password() if generated else body.password
    try:
        out = offsite.setup(state.ctx, state.conn(), body.repo, password)
    except (ExportError, offsite.OffsiteError) as exc:
        raise HTTPException(400, str(exc))
    # A generated password is shown this once, to be written down (it can be shown again below).
    return {**out, "password": password if generated else None}


@router.post("/offsite/run")
def offsite_run():
    from ..scheduler import start_offsite

    state = get_state()
    ctx, conn = state.ctx, state.conn()
    if not ctx.settings.offsite_repo:
        raise HTTPException(400, "set up the off-site copy first")
    if offsite.find_restic(ctx) is None:
        raise HTTPException(400, "restic is not installed")
    if _running(conn):
        raise HTTPException(409, "an off-site copy is already running")
    return {"job_id": start_offsite(ctx, conn)}


_checking = threading.Event()


@router.post("/offsite/verify")
def offsite_verify():
    ctx = get_state().ctx
    if not ctx.settings.offsite_repo:
        raise HTTPException(400, "set up the off-site copy first")
    if _checking.is_set():
        return {"started": False}
    _checking.set()

    def work():
        conn = ctx.connect()
        try:
            offsite.verify(ctx, conn)
        except Exception:
            pass
        finally:
            conn.close()
            _checking.clear()

    threading.Thread(target=work, daemon=True, name="offsite-check").start()
    return {"started": True}


@router.get("/offsite/snapshots")
def offsite_snapshots():
    try:
        return {"snapshots": offsite.snapshots(get_state().ctx)}
    except offsite.OffsiteError as exc:
        raise HTTPException(502, str(exc))


@router.get("/offsite/password")
def offsite_password():
    return {"password": offsite.reveal_password(get_state().ctx)}
