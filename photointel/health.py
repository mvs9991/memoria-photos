"""Health: the things that quietly go wrong when one computer is the family's photo service.

`check()` looks at the library as it is now and returns a list of problems, worst first:
a photo folder that cannot be reached (drive unplugged), a disk filling up, no backup / an old
backup / a failed one, a phone whose backup app has not connected for a week, the server
restarting itself repeatedly, an HTTPS certificate about to lapse, the off-site copy.

The app shows them to owners; `notify()` can also send each new one to a webhook the owner
chose (an ntfy topic, which has a phone app — self-hosted, or ntfy.sh, which is someone
else's server; or a Discord webhook). Off unless set. Messages name folders only by their last
part and never carry photos or their contents.

Thresholds are judgement, not measurement: free space below 2 GB is an error and below 20 GB
*and* 10 % a warning; a phone is "quiet" after 7 days; a backup is late at 1.5× its interval.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.request
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

GB = 1 << 30
DISK_ERROR = 2 * GB
DISK_WARN = 20 * GB
DISK_WARN_FRACTION = 0.10
PHONE_QUIET_S = 7 * 86400
CRASHES_PER_DAY = 3
RESEND_S = {"error": 86400, "warn": 7 * 86400}     # while a problem lasts, send it again after


def _p(key, level, title, detail, fix=""):
    return {"key": key, "level": level, "title": title, "detail": detail, "fix": fix}


def _day(ts: float) -> str:
    return time.strftime("%d %b %Y", time.localtime(ts))


def _size(n: float) -> str:
    return f"{n / GB:,.1f} GB" if n >= GB else f"{n / (1 << 20):,.0f} MB"


def upload_folder(ctx) -> Path:
    return Path(ctx.settings.upload_folder) if ctx.settings.upload_folder else ctx.paths.data / "uploads"


def check(ctx, conn, now: float | None = None, disk_usage=shutil.disk_usage, isdir=os.path.isdir) -> list[dict]:
    now = now or time.time()
    s = ctx.settings
    out: list[dict] = []

    # ---- photo folders
    reachable = []
    for r in conn.execute("SELECT id, path FROM roots ORDER BY id"):
        if isdir(r["path"]):
            reachable.append((r["path"], "photos"))
        else:
            out.append(_p(f"root:{r['id']}", "error", "A photo folder can't be reached",
                          f"“{Path(r['path']).name or r['path']}” is missing. Its photos are hidden until it is back.",
                          "Plug the drive back in (or reconnect the network share); nothing is lost."))

    # ---- disk space, once per drive, naming what lives there
    places = [(str(ctx.paths.data), "Memoria's data"), (str(upload_folder(ctx)), "phone uploads"), *reachable]
    if s.backup_folder and isdir(s.backup_folder):
        places.append((s.backup_folder, "the backup"))
    drives: dict = {}
    for path, what in places:
        probe = path
        while probe and not isdir(probe):                   # an upload folder not made yet
            parent = os.path.dirname(probe.rstrip("\\/"))
            probe = parent if parent != probe else ""
        if not probe:
            continue
        try:
            dev = os.stat(probe).st_dev
            usage = disk_usage(probe)
        except OSError:
            continue
        entry = drives.setdefault(dev, {"usage": usage, "what": [], "path": probe})
        if what not in entry["what"]:
            entry["what"].append(what)
    for dev, d in drives.items():
        free, total = d["usage"].free, d["usage"].total
        anchor = Path(d["path"]).anchor or d["path"]
        holds = ", ".join(d["what"])
        if free < DISK_ERROR:
            out.append(_p(f"disk:{dev}", "error", f"{anchor} is almost full",
                          f"{_size(free)} left on the drive that holds {holds}. New photos and backups will fail.",
                          "Free up space, or move uploads/backups to a bigger drive in Settings."))
        elif free < DISK_WARN and total and free / total < DISK_WARN_FRACTION:
            out.append(_p(f"disk:{dev}", "warn", f"{anchor} is getting full",
                          f"{_size(free)} left ({free / total:.0%}) on the drive that holds {holds}.",
                          "Free up space before it runs out."))

    # ---- backup
    from .engine.backup import last_backup

    lb = last_backup(conn)
    if not s.backup_folder:
        out.append(_p("backup:none", "warn", "No backup is set up",
                      "Your photos are only on this computer. A failed disk would lose them.",
                      "Settings > Backup: choose another drive."))
    elif not isdir(s.backup_folder):
        out.append(_p("backup:unreachable", "error", "The backup drive isn't connected",
                      f"{Path(s.backup_folder).name or s.backup_folder} can't be found, so backups are not happening.",
                      "Plug the backup drive in; the next backup starts by itself."))
    else:
        every = max(s.backup_every_days, 0)
        late_after = every * 86400 * 1.5 if every else 30 * 86400
        last_ok = lb["finished_at"] if lb else None
        if last_ok is None:
            out.append(_p("backup:never", "warn", "No backup has finished yet",
                          "A backup drive is set, but no backup has completed.",
                          "Settings > Backup > Back up now."))
        elif now - last_ok > late_after:
            out.append(_p("backup:late", "warn", "The backup is out of date",
                          f"The last backup finished on {_day(last_ok)}.", "Settings > Backup > Back up now."))
        latest = conn.execute("SELECT status, error, message, finished_at FROM jobs WHERE kind = 'backup' "
                              "ORDER BY id DESC LIMIT 1").fetchone()
        if latest and latest["status"] == "failed" and (last_ok is None or (latest["finished_at"] or now) > last_ok):
            out.append(_p("backup:failed", "error", "The last backup failed",
                          (latest["error"] or latest["message"] or "no reason recorded")[:300],
                          "Check the backup drive, then Settings > Backup > Back up now."))
        if lb and lb.get("hash_mismatches"):
            out.append(_p("backup:mismatch", "error", "Backup copies did not match the originals",
                          f"{len(lb['hash_mismatches'])} copied files did not match their fingerprint — a failing "
                          "disk or cable.", "Check the backup drive's health; run the backup again."))

    # ---- phones that stopped backing up
    for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'dav_seen:%'"):
        who, seen = r["key"].split(":", 1)[1], float(r["value"] or 0)
        if seen and now - seen > PHONE_QUIET_S:
            out.append(_p(f"phone:{who}", "warn", f"{who}'s phone hasn't backed up for {int((now - seen) // 86400)} days",
                          f"Its backup app last connected on {_day(seen)}.",
                          "Open the backup app on that phone; check it is still set up and on the home Wi-Fi."))

    # ---- the server itself
    from .service import restarts_since

    crashes = restarts_since(ctx.paths.data, now - 86400)
    if crashes >= CRASHES_PER_DAY:
        out.append(_p("service:crashing", "warn", f"Memoria restarted itself {crashes} times today",
                      "It keeps stopping unexpectedly; the keeper starts it again each time.",
                      "The reason is in logs/server-output.log in the data folder."))

    # ---- HTTPS
    if s.https_enabled:
        from . import tls

        files = tls.cert_files(ctx.paths.data)
        info = tls.cert_info(files[0]) if files else None
        if info is None:
            out.append(_p("https:missing", "error", "HTTPS is on but there is no certificate",
                          "Phones using the https address can't connect.", "Settings > Access > Get certificate."))
        elif info["not_after"] < now:
            out.append(_p("https:expired", "error", "The HTTPS certificate has expired",
                          f"It ended on {_day(info['not_after'])}; phones will refuse to connect.",
                          "Settings > Access > Renew certificate."))
        elif info["not_after"] - now < 14 * 86400:
            out.append(_p("https:expiring", "warn", "The HTTPS certificate expires soon",
                          f"On {_day(info['not_after'])}. Renewing by itself did not work.",
                          "Settings > Access > Renew certificate."))

    # ---- off-site copy
    from .engine import offsite

    out.extend(offsite.problems(ctx, conn, now))

    out.sort(key=lambda p: 0 if p["level"] == "error" else 1)
    return out


# ---------------------------------------------------------------- snoozing and sending

def _snoozed(conn) -> dict:
    try:
        return json.loads(db.get_meta(conn, "alerts_snoozed") or "{}")
    except ValueError:
        return {}


def snooze(conn, key: str, days: int, now: float | None = None) -> None:
    snoozed = {k: v for k, v in _snoozed(conn).items() if v > (now or time.time())}
    snoozed[key] = (now or time.time()) + days * 86400
    db.set_meta(conn, "alerts_snoozed", json.dumps(snoozed))
    conn.commit()


def visible(conn, problems: list[dict], now: float | None = None) -> tuple[list[dict], int]:
    """-> (problems not snoozed, how many are snoozed)."""
    until = _snoozed(conn)
    now = now or time.time()
    shown = [p for p in problems if until.get(p["key"], 0) <= now]
    return shown, len(problems) - len(shown)


def notify(ctx, conn, problems: list[dict], now: float | None = None, post=None) -> list[str]:
    """Send each problem that is new (or unsent for a day) to the webhook. -> keys sent."""
    url = ctx.settings.alert_webhook.strip()
    if not url:
        return []
    now = now or time.time()
    post = post or _post
    try:
        sent = json.loads(db.get_meta(conn, "alerts_sent") or "{}")
    except ValueError:
        sent = {}
    current = {p["key"] for p in problems}
    sent = {k: v for k, v in sent.items() if k in current}           # fixed ones may alert again later
    shown, _ = visible(conn, problems, now)
    out = []
    for p in shown:
        if now - sent.get(p["key"], 0) < RESEND_S[p["level"]]:
            continue
        try:
            post(url, "Memoria: " + p["title"], p["detail"] + (f"\n{p['fix']}" if p["fix"] else ""), p["level"])
        except Exception as exc:
            log.warning("Could not send an alert to the webhook: %s", exc)
            break                                                        # try again next time
        sent[p["key"]] = now
        out.append(p["key"])
    db.set_meta(conn, "alerts_sent", json.dumps(sent))
    conn.commit()
    return out


def _post(url: str, title: str, body: str, level: str) -> None:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the alert address must start with http:// or https://")
    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        data = json.dumps({"content": f"**{title}**\n{body}"}).encode()
        headers = {"Content-Type": "application/json"}
    else:                                                               # ntfy and anything taking plain text
        data = body.encode("utf-8")
        headers = {"Title": title.encode("ascii", "replace").decode(), "Tags": "warning" if level == "warn" else "rotating_light",
                   "Content-Type": "text/plain; charset=utf-8"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:               # noqa: S310 - the owner's own address
        resp.read()
