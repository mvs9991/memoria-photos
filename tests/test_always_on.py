"""Always-on: the keeper and auto-start, HTTPS through Tailscale, health alerts, the off-site copy."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from photointel import autostart, db, health, service, tls
from photointel.engine import offsite
from photointel.engine.xmp import ExportError
from photointel.pipeline.scanner import ensure_root

# A self-signed certificate for pc.tail1234.ts.net, valid until 24 Sep 2036 (made with openssl for
# these tests; nothing trusts it).
TEST_CERT = """-----BEGIN CERTIFICATE-----
MIIBrzCCAVSgAwIBAgIUYPrBaJSF+j7yBQSj8xi8X6oxnU4wCgYIKoZIzj0EAwIw
HTEbMBkGA1UEAwwScGMudGFpbDEyMzQudHMubmV0MB4XDTI2MDkyNzExMjIxN1oX
DTM2MDkyNDExMjIxN1owHTEbMBkGA1UEAwwScGMudGFpbDEyMzQudHMubmV0MFkw
EwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgjAqaKrMI1dNESrKrRyQp3UbMGmj/WXb
YSKTOlEIntZx4znJpru8d2B45Heamvm+OwxKtXUFhbm3uvgHniMKXqNyMHAwHQYD
VR0OBBYEFGCc43Lw9+ty3SbhJb2GyrwU5qJ6MB8GA1UdIwQYMBaAFGCc43Lw9+ty
3SbhJb2GyrwU5qJ6MA8GA1UdEwEB/wQFMAMBAf8wHQYDVR0RBBYwFIIScGMudGFp
bDEyMzQudHMubmV0MAoGCCqGSM49BAMCA0kAMEYCIQCji+XhE8Oo9L7lNGqL4QVi
h+hZjSbjSy7TlDvEmJCVpwIhAN0DAgKBU1UCY3lBxWyFjly8ATtxudld155pSmCT
Age6
-----END CERTIFICATE-----
"""
CERT_END = 2105868137          # 24 Sep 2036 11:22:17 UTC


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    return create_app(ctx)


def _owner_with_password(app) -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/password", json={"new": "owner-pass"})
    return c


# ----------------------------------------------------------------- the keeper

def test_the_keeper_restarts_after_a_crash_but_not_after_a_stop_or_a_refusal():
    assert service.decide(0, 5, 0) == ("stop", 0)
    assert service.decide(service.CONFIG_ERROR, 5, 2) == ("give-up", 2)          # it would only fail again
    assert service.decide(service.RESTART_CODE, 5, 3) == ("restart", 0)         # asked for: at once
    assert service.decide(1, 5, 0) == ("restart-later", 1)
    assert service.decide(1, 5, 1) == ("restart-later", 2)
    assert service.decide(-9, service.HEALTHY_AFTER_S + 1, 4) == ("restart-later", 1)   # ran well: start over
    assert [service.next_delay(n) for n in range(1, 8)] == [3, 10, 30, 60, 300, 300, 300]


def test_the_keeper_loop(tmp_path):
    codes = iter([1, 1, service.RESTART_CODE, 0])
    seen, slept = [], []

    def spawn(cmd, data):
        seen.append(cmd)
        return next(codes)

    assert service.run(tmp_path, ["--port", "9000"], spawn=spawn, sleep=slept.append) == "stopped"
    assert len(seen) == 4 and slept == [3, 10]
    assert seen[0][-4:] == ["serve", "--port", "9000"][-3:] or seen[0][-3:] == ["serve", "--port", "9000"]
    assert "--data" in seen[0] and str(tmp_path) in seen[0]
    state = service.read_state(tmp_path)
    assert state["status"] == "stopped" and len(state["restarts"]) == 2 and state["last_exit"] == 0

    slept.clear()
    refusals = []

    def refuse(cmd, data):
        refusals.append(cmd)
        assert len(refusals) < 5, "the keeper kept restarting a server that refused to start"
        return service.CONFIG_ERROR

    assert service.run(tmp_path, [], spawn=refuse, sleep=slept.append) == "config-error"
    assert len(refusals) == 1 and slept == [] and service.read_state(tmp_path)["status"] == "config-error"


def test_a_restart_is_only_offered_under_the_keeper(monkeypatch):
    monkeypatch.delenv(service.SUPERVISED_ENV, raising=False)
    with pytest.raises(RuntimeError):
        service.request_restart()
    assert not service.supervised()


def test_restart_api_says_when_it_cannot(app):
    r = TestClient(app).post("/api/service/restart")
    assert r.status_code == 409


# ----------------------------------------------------------------- auto-start

@pytest.mark.skipif(os.name != "nt", reason="the Windows start-up entry")
def test_autostart_on_sign_in_writes_and_removes_only_its_own_entry(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("PHOTOINTEL_MODELS", r"D:\shared models")
    monkeypatch.setattr(autostart, "_task_exists", lambda: False)
    out = autostart.install(ctx.paths.data)
    entry = tmp_path / "appdata" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Memoria.cmd"
    assert out == {"method": "sign-in", "where": str(entry)} and entry.exists()
    text = entry.read_text(encoding="utf-8")
    assert f'cd /d "{service.PROJECT_ROOT}"' in text
    assert f'--data "{ctx.paths.data}" run' in text
    assert 'set "PHOTOINTEL_MODELS=D:\\shared models"' in text          # a boot task does not see the user's env
    assert "pythonw.exe" in text                                        # no console window
    assert autostart.status() == {"sign_in": True, "boot": False}
    assert autostart.remove() == [str(entry)] and not entry.exists()
    assert autostart.status() == {"sign_in": False, "boot": False}


def test_linux_and_mac_entries_run_the_keeper(ctx, monkeypatch):
    monkeypatch.setenv("PHOTOINTEL_GEO", "/srv/geo")
    unit = autostart.systemd_unit(ctx.paths.data)
    assert f'--data "{ctx.paths.data}" run' in unit and "Restart=on-failure" in unit
    assert 'Environment="PHOTOINTEL_GEO=/srv/geo"' in unit
    plist = plistlib.loads(autostart.launchd_plist(ctx.paths.data).encode())
    assert plist["ProgramArguments"][-3:] == ["--data", str(ctx.paths.data), "run"]
    assert plist["RunAtLoad"] is True and plist["EnvironmentVariables"] == {"PHOTOINTEL_GEO": "/srv/geo"}


# ----------------------------------------------------------------- serve: http and https together

def _serve_args(**kw):
    return SimpleNamespace(**{"host": None, "port": None, "insecure": False, "ssl_cert": None, "ssl_key": None, **kw})


def _with_cert(ctx):
    cert, key = tls.cert_paths(ctx.paths.data)
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_text(TEST_CERT)
    key.write_text("not a real key")


def test_https_is_served_beside_http_and_needs_a_password(ctx, monkeypatch):
    from photointel import cli

    calls = []
    monkeypatch.setattr(cli, "_serve_together", lambda app, servers: calls.append(servers))
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append([kw]))
    _with_cert(ctx)
    ctx.settings.https_enabled = True
    with pytest.raises(SystemExit) as e:
        cli.cmd_serve(ctx, _serve_args())
    assert e.value.code == 2                                    # reachable from other devices: not without one
    from photointel import auth

    ctx.settings.access_password_hash = auth.hash_password("owner-pass")
    cli.cmd_serve(ctx, _serve_args())
    (servers,) = calls
    assert [(h, p) for h, p, _ in servers] == [("127.0.0.1", 8765), ("0.0.0.0", 8443)]
    assert servers[0][2] == {} and servers[1][2]["ssl_certfile"].endswith("cert.pem")
    calls.clear()
    ctx.settings.https_enabled = False                          # off: plain http only, as before
    cli.cmd_serve(ctx, _serve_args(port=9000))
    assert calls == [[{"host": "127.0.0.1", "port": 9000, "log_level": "warning"}]]


# ----------------------------------------------------------------- Tailscale certificates

FAKE_TAILSCALE = r'''
import json, os, shutil, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_TS", "ok")
if args[:2] == ["status", "--json"]:
    print(json.dumps({"BackendState": "Running", "Self": {"DNSName": "pc.tail1234.ts.net."},
                      "CertDomains": [] if mode == "no-https" else ["pc.tail1234.ts.net"]}))
elif args[0] == "cert":
    if mode == "fail":
        sys.stderr.write("500 Internal Server Error: your tailnet does not have HTTPS enabled")
        sys.exit(1)
    cert = args[args.index("--cert-file") + 1]
    key = args[args.index("--key-file") + 1]
    shutil.copyfile(os.environ["FAKE_TS_CERT"], cert)
    open(key, "w").write("key")
'''


@pytest.fixture
def fake_tailscale(tmp_path, monkeypatch):
    exe = tmp_path / "tailscale.py"
    exe.write_text(FAKE_TAILSCALE)
    pem = tmp_path / "issued.pem"
    pem.write_text(TEST_CERT)
    monkeypatch.setenv("PHOTOINTEL_TAILSCALE", str(exe))
    monkeypatch.setenv("FAKE_TS_CERT", str(pem))
    return exe


def test_tailscale_status_and_certificate(ctx, fake_tailscale, monkeypatch):
    st = tls.tailscale_status()
    assert st == {"installed": True, "running": True, "name": "pc.tail1234.ts.net", "https_ready": True,
                  "domain": "pc.tail1234.ts.net"}
    info = tls.fetch_cert(ctx.paths.data, st["domain"])
    assert info == {"not_after": CERT_END, "names": ["pc.tail1234.ts.net"]}
    assert tls.cert_files(ctx.paths.data)
    assert tls.https_url(ctx.settings, info) == "https://pc.tail1234.ts.net:8443"
    assert not tls.renew_due(ctx.paths.data, now=CERT_END - 60 * 86400)
    assert tls.renew_due(ctx.paths.data, now=CERT_END - 10 * 86400)

    monkeypatch.setenv("FAKE_TS", "fail")                         # a failed renewal keeps the old certificate
    with pytest.raises(RuntimeError, match="HTTPS enabled"):
        tls.fetch_cert(ctx.paths.data, st["domain"])
    cert, key = tls.cert_paths(ctx.paths.data)
    assert tls.cert_info(cert)["not_after"] == CERT_END and key.read_text() == "key"
    assert not list(cert.parent.glob("*.new"))
    monkeypatch.setenv("FAKE_TS", "no-https")
    assert tls.tailscale_status()["https_ready"] is False


def test_https_setup_from_the_app(ctx, app, fake_tailscale):
    c = TestClient(app)
    assert c.post("/api/https/setup").status_code == 400          # no password yet
    c = _owner_with_password(app)
    r = c.post("/api/https/setup")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] and body["url"] == "https://pc.tail1234.ts.net:8443" and body["restarting"] is False
    assert json.loads((ctx.paths.data / "settings.json").read_text())["https_enabled"] is True
    assert c.post("/api/https/off").json()["enabled"] is False


# ----------------------------------------------------------------- health

class Usage(SimpleNamespace):
    pass


def _roots(ctx, *paths):
    conn = ctx.connect()
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)
        ensure_root(conn, Path(p))
    conn.commit()
    return conn


def keys(problems):
    return {p["key"]: p["level"] for p in problems}


def test_health_finds_what_quietly_goes_wrong(ctx, tmp_path):
    conn = _roots(ctx, tmp_path / "photos", tmp_path / "gone")
    shutil.rmtree(tmp_path / "gone")
    roomy = lambda p: Usage(total=4000 << 30, used=0, free=900 << 30)          # noqa: E731
    got = keys(health.check(ctx, conn, disk_usage=roomy))
    root_gone = next(k for k in got if k.startswith("root:"))
    assert got[root_gone] == "error"
    assert got["backup:none"] == "warn" and got["offsite:none"] == "warn"
    assert not any(k.startswith("disk:") for k in got)

    full = lambda p: Usage(total=500 << 30, used=0, free=1 << 30)              # noqa: E731
    disk = [p for p in health.check(ctx, conn, disk_usage=full) if p["key"].startswith("disk:")]
    assert len(disk) == 1 and disk[0]["level"] == "error"                    # one drive, said once
    assert "photos" in disk[0]["detail"] and "phone uploads" in disk[0]["detail"]
    tight = lambda p: Usage(total=500 << 30, used=0, free=15 << 30)            # noqa: E731
    assert [p["level"] for p in health.check(ctx, conn, disk_usage=tight) if p["key"].startswith("disk:")] == ["warn"]

    # backups: missing drive, never finished, late, failed
    ctx.settings.backup_folder = str(tmp_path / "usb")
    assert keys(health.check(ctx, conn, disk_usage=roomy))["backup:unreachable"] == "error"
    (tmp_path / "usb").mkdir()
    assert "backup:never" in keys(health.check(ctx, conn, disk_usage=roomy))
    now = time.time()
    db.set_meta(conn, "last_backup", json.dumps({"finished_at": now - 20 * 86400}))
    conn.commit()
    got = keys(health.check(ctx, conn, now=now, disk_usage=roomy))
    assert "backup:late" in got and "backup:never" not in got
    conn.execute("INSERT INTO jobs(kind, status, error, created_at, finished_at) VALUES ('backup', 'failed', "
                 "'E:\\ is not ready', ?, ?)", (now - 60, now - 60))
    conn.commit()
    failed = [p for p in health.check(ctx, conn, now=now, disk_usage=roomy) if p["key"] == "backup:failed"]
    assert failed and failed[0]["level"] == "error" and "not ready" in failed[0]["detail"]
    assert health.check(ctx, conn, now=now, disk_usage=roomy)[0]["level"] == "error"      # worst first

    # a phone gone quiet; another that is fine
    db.set_meta(conn, "dav_seen:Priya", now - 9 * 86400)
    db.set_meta(conn, "dav_seen:Sanjay", now - 3600)
    conn.commit()
    got = keys(health.check(ctx, conn, now=now, disk_usage=roomy))
    assert got.get("phone:Priya") == "warn" and "phone:Sanjay" not in got

    # the server crashing; HTTPS without a certificate
    service._write_state(ctx.paths.data, {"restarts": [now - 100, now - 200, now - 300]})
    ctx.settings.https_enabled = True
    got = keys(health.check(ctx, conn, now=now, disk_usage=roomy))
    assert got["service:crashing"] == "warn" and got["https:missing"] == "error"
    conn.close()


def test_a_webhook_hears_each_problem_once_until_it_is_fixed(ctx, tmp_path):
    conn = ctx.connect()
    sent = []
    post = lambda url, title, body, level: sent.append((title, level))           # noqa: E731
    problems = [health._p("backup:failed", "error", "The last backup failed", "x"),
                health._p("offsite:none", "warn", "No copy outside this house", "y")]
    assert health.notify(ctx, conn, problems, post=post) == []                   # no address: nothing
    ctx.settings.alert_webhook = "https://ntfy.example/memoria"
    now = time.time()
    assert health.notify(ctx, conn, problems, now=now, post=post) == ["backup:failed", "offsite:none"]
    assert health.notify(ctx, conn, problems, now=now + 3600, post=post) == []
    assert health.notify(ctx, conn, problems, now=now + 25 * 3600, post=post) == ["backup:failed"]   # errors: daily
    assert health.notify(ctx, conn, problems, now=now + 8 * 86400, post=post) == ["backup:failed", "offsite:none"]
    health.notify(ctx, conn, problems[1:], now=now + 8 * 86400 + 60, post=post)  # the backup was fixed…
    assert health.notify(ctx, conn, problems, now=now + 8 * 86400 + 120, post=post) == ["backup:failed"]  # …broke again
    health.snooze(conn, "backup:failed", 7, now=now + 9 * 86400)
    assert health.notify(ctx, conn, problems, now=now + 9 * 86400 + 60, post=post) == []
    shown, snoozed = health.visible(conn, problems, now=now + 9 * 86400 + 60)
    assert [p["key"] for p in shown] == ["offsite:none"] and snoozed == 1
    assert sent[0] == ("Memoria: The last backup failed", "error")
    conn.close()


def test_a_backup_app_checking_in_is_remembered(ctx, app):
    owner = _owner_with_password(app)
    owner.post("/api/accounts/enable", json={"username": "Sanjay"})
    owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    r = TestClient(app).request("PROPFIND", "/dav/", headers={"Depth": "1"}, auth=("Priya", "priya-pass"))
    assert r.status_code == 207
    conn = ctx.connect()
    assert abs(float(db.get_meta(conn, "dav_seen:Priya")) - time.time()) < 60
    conn.close()


def test_the_always_on_screens_are_the_owners(app):
    owner = _owner_with_password(app)
    owner.post("/api/accounts/enable", json={"username": "Sanjay"})
    owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    priya = TestClient(app)
    priya.post("/api/auth/login", json={"username": "Priya", "password": "priya-pass"})
    for method, path in (("GET", "/api/alerts"), ("GET", "/api/service"), ("GET", "/api/https"),
                         ("GET", "/api/offsite"), ("GET", "/api/offsite/password"), ("POST", "/api/service/restart"),
                         ("POST", "/api/offsite/run"), ("POST", "/api/https/setup")):
        assert priya.request(method, path).status_code == 403, path
    assert owner.get("/api/alerts").status_code == 200


# ----------------------------------------------------------------- the off-site copy

FAKE_RESTIC = r'''
import json, os, sys
log = os.environ["FAKE_RESTIC_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(),
                        "repo": os.environ.get("RESTIC_REPOSITORY")}) + "\n")
cmd = sys.argv[1]
if cmd == "cat":
    sys.exit(0 if os.path.exists(os.path.join(os.environ["RESTIC_REPOSITORY"], "config")) else 1)
if cmd == "init":
    open(os.path.join(os.environ["RESTIC_REPOSITORY"], "config"), "w").write("x")
elif cmd == "backup":
    print(json.dumps({"message_type": "status", "percent_done": 0.5, "files_done": 1, "total_files": 2}))
    print(json.dumps({"message_type": "summary", "snapshot_id": "ab" * 32, "files_new": 2, "files_changed": 0,
                      "total_files_processed": 2, "data_added": 100}))
'''


@pytest.fixture
def fake_restic(ctx, tmp_path, monkeypatch):
    exe = tmp_path / "restic.py"
    exe.write_text(FAKE_RESTIC)
    log = tmp_path / "restic-calls.jsonl"
    monkeypatch.setenv("PHOTOINTEL_RESTIC", str(exe))
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(log))
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_each_folder_is_copied_by_its_own_name(ctx, tmp_path, fake_restic):
    """Named in full, restic records C:\\ and C:\\Users with their Windows permissions and a restore
    recreates them — found with the real restic: the restored folder was locked against its user."""
    conn = _roots(ctx, tmp_path / "Pictures" / "Family", tmp_path / "D" / "Camera")
    offsite.setup(ctx, conn, str(tmp_path / "away"), "correct horse battery")
    out = offsite.run(ctx, conn)
    assert out["folders"] == 2 and len(out["snapshots"]) == 3 and out["files_new"] == 6
    backups = [c for c in fake_restic() if c["args"][0] == "backup"]
    assert [(Path(c["cwd"]), c["args"][-1]) for c in backups] == [
        (tmp_path / "Pictures", "Family"), (tmp_path / "D", "Camera"),
        (ctx.paths.data / "offsite" / "staging", "memoria")]
    assert all("--exclude" in c["args"] and ".memoria-trash" in c["args"] for c in backups)
    assert (ctx.paths.data / "offsite" / "staging" / "memoria" / "library.db").exists()
    assert offsite.last(conn)["finished_at"] > 0
    conn.close()


def test_offsite_setup_rules(ctx, tmp_path, fake_restic):
    conn = _roots(ctx, tmp_path / "photos")
    with pytest.raises(ExportError):
        offsite.setup(ctx, conn, str(tmp_path / "photos" / "offsite"), "a long enough password")
    with pytest.raises(ExportError):
        offsite.setup(ctx, conn, str(ctx.paths.data / "offsite-copy"), "a long enough password")
    with pytest.raises(offsite.OffsiteError):
        offsite.setup(ctx, conn, str(tmp_path / "away"), "short")
    assert offsite.check_location(ctx, conn, "sftp:me@nas:/backups") == "sftp:me@nas:/backups"
    out = offsite.setup(ctx, conn, str(tmp_path / "away"), "a long enough password")
    assert out["created"] and ctx.settings.offsite_repo == str((tmp_path / "away").resolve())
    assert offsite.setup(ctx, conn, str(tmp_path / "away"), "a long enough password")["created"] is False
    assert offsite.reveal_password(ctx) == "a long enough password"
    conn.close()


def test_a_restore_goes_only_into_an_empty_folder_outside_the_library(ctx, tmp_path, fake_restic):
    conn = _roots(ctx, tmp_path / "photos")
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x.txt").write_text("x")
    for target in (tmp_path / "photos" / "restored", ctx.paths.data / "restored", tmp_path / "busy"):
        with pytest.raises(ExportError):
            offsite.restore_snapshot(ctx, conn, str(target))
    with pytest.raises(offsite.OffsiteError):
        offsite.restore_snapshot(ctx, conn, str(tmp_path / "new"), snapshot="latest; rm -rf /")
    conn.close()


def test_only_folder_date_errors_are_harmless_after_a_restore():
    import subprocess

    ok = subprocess.CompletedProcess([], 1, "", 'ignoring error for \\C\\Users: failed to restore timestamp of '
                                              '"x": Access is denied.\nFatal: There were 1 errors\n')
    assert offsite._harmless_restore_errors(ok) and len(offsite._harmless_restore_errors(ok)) == 1
    bad = subprocess.CompletedProcess([], 1, "", "ignoring error for \\photos\\a.jpg: open: Access is denied.\n"
                                               "Fatal: There were 1 errors\n")
    assert offsite._harmless_restore_errors(bad) is None
    verify_failed = subprocess.CompletedProcess([], 1, "", 'ignoring error for \\C: failed to restore timestamp\n'
                                                         "error: verification of a.jpg failed\n")
    assert offsite._harmless_restore_errors(verify_failed) is None


def test_offsite_schedule_and_problems(ctx, tmp_path):
    s = ctx.settings
    now = time.time()
    assert not offsite.due(s, now, None, None, False)                      # not set up
    s.offsite_repo = str(tmp_path / "away")
    assert offsite.due(s, now, None, None, False)
    assert not offsite.due(s, now, now - 86400, None, False)              # done yesterday
    assert offsite.due(s, now, now - 8 * 86400, None, False)
    assert not offsite.due(s, now, now - 8 * 86400, now - 3600, False)    # tried an hour ago and failed: wait
    assert not offsite.due(s, now, None, None, True)                      # already running
    conn = ctx.connect()
    got = keys(offsite.problems(ctx, conn, now))
    assert got.get("offsite:never") == "warn"
    db.set_meta(conn, "offsite_last", json.dumps({"finished_at": now - 30 * 86400}))
    db.set_meta(conn, "offsite_check", json.dumps({"at": now, "ok": False, "detail": "pack 1a2b is damaged"}))
    conn.commit()
    got = keys(offsite.problems(ctx, conn, now))
    assert got["offsite:late"] == "warn" and got["offsite:check"] == "error"
    conn.close()


def test_offsite_setup_from_the_app_shows_a_made_password_once(ctx, app, tmp_path, fake_restic):
    c = TestClient(app)
    r = c.post("/api/offsite/setup", json={"repo": str(tmp_path / "away")})
    assert r.status_code == 200, r.text
    made = r.json()["password"]
    assert len(made) == 23 and made.count("-") == 3
    assert c.get("/api/offsite/password").json()["password"] == made
    r = c.post("/api/offsite/setup", json={"repo": str(tmp_path / "away2"), "password": "mine and long enough"})
    assert r.json()["password"] is None                                    # the user's own is not echoed
    assert c.post("/api/offsite/setup", json={"repo": str(tmp_path / "x"), "password": "short"}).status_code == 400


def _real_restic():
    for c in (os.environ.get("PHOTOINTEL_TEST_RESTIC"), shutil.which("restic")):
        if c and Path(c).is_file():
            return c
    return None


@pytest.mark.skipif(_real_restic() is None, reason="restic not installed (set PHOTOINTEL_TEST_RESTIC)")
def test_with_the_real_restic_a_copy_restores_verified_and_writable(ctx, library, tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTOINTEL_RESTIC", _real_restic())
    conn = _roots(ctx, library)
    offsite.setup(ctx, conn, str(tmp_path / "away"), offsite.new_password())
    first = offsite.run(ctx, conn)
    assert first["files_new"] == sum(1 for p in library.rglob("*") if p.is_file()) + 2     # + library.db, settings
    again = offsite.run(ctx, conn)
    assert again["files_new"] == 0
    assert offsite.verify(ctx, conn)["ok"]
    out = offsite.restore_snapshot(ctx, conn, str(tmp_path / "back"))
    assert sorted(p.name for p in (tmp_path / "back").iterdir()) == sorted([library.name, "memoria"])
    for src in library.rglob("*"):
        if src.is_file():
            assert (tmp_path / "back" / library.name / src.relative_to(library)).read_bytes() == src.read_bytes()
    probe = tmp_path / "back" / library.name / "written-after-restore.txt"
    probe.write_text("the restored folder belongs to its user")
    assert out["warnings"] == []
    conn.close()


# ----------------------------------------------------------------- the scheduler's chores

def test_upkeep_keeps_awake_checks_health_and_starts_the_offsite_copy(ctx, monkeypatch, tmp_path):
    from photointel import scheduler

    awake, started, sent = [], [], []
    monkeypatch.setattr(service, "keep_awake", lambda on: awake.append(on))
    monkeypatch.setattr(scheduler, "start_offsite", lambda c, conn: started.append(1))
    monkeypatch.setattr(health, "_post", lambda *a: sent.append(a[1]))
    ctx.settings.keep_awake = True
    ctx.settings.offsite_repo = str(tmp_path / "away")
    ctx.settings.alert_webhook = "https://ntfy.example/t"
    conn = ctx.connect()
    now = time.time()
    scheduler._upkeep(ctx, conn, now, busy=False, backup_running=False)
    assert awake == [True] and started == [1]
    assert "Memoria: No backup is set up" in sent
    n = len(sent)
    scheduler._upkeep(ctx, conn, now + 60, busy=False, backup_running=False)
    assert len(sent) == n and started == [1]                  # health every 15 minutes; the copy waits its retry
    conn.close()
