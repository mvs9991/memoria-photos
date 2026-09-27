"""Starting Memoria with the computer, through the operating system's own mechanism.

Windows, default ("sign-in"): a small .cmd in the user's Startup folder. No admin rights;
  Memoria starts when this user signs in to Windows.
Windows, --at-boot: a Task Scheduler task that runs at boot as SYSTEM, before anyone signs
  in. Needs an administrator terminal. SYSTEM does not see mapped network drives.
Linux: a systemd *user* unit (`loginctl enable-linger` makes it start before sign-in).
macOS: a launchd agent in ~/Library/LaunchAgents. (Linux and macOS: written and checked
  as text, never run on those systems here.)

Every method runs the same launcher, <data>/memoria-start.cmd (Windows) or the equivalent
command line, which starts the keeper (`photointel run`, see service.py). The environment
variables that choose where models and place data live are copied into it, because a task
running at boot does not see the user's environment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .service import PROJECT_ROOT

TASK_NAME = "Memoria"
UNIT = "memoria.service"
PLIST = "com.memoria.photos.plist"
CARRIED_ENV = ("PHOTOINTEL_MODELS", "PHOTOINTEL_GEO", "PHOTOINTEL_DEVICE")      # HF_HOME is set from --data


def _python(windowless: bool) -> Path:
    exe = Path(sys.executable)
    if os.name == "nt" and windowless and exe.with_name("pythonw.exe").exists():
        return exe.with_name("pythonw.exe")
    return exe


def startup_folder() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launcher_text(data: Path) -> str:
    """The Windows launcher: go to the code, carry the environment, start the keeper windowless."""
    lines = ["@echo off",
             "rem Starts Memoria's keeper (made by `python -m photointel autostart on`; remove with `autostart off`).",
             f'cd /d "{PROJECT_ROOT}"']
    for k in CARRIED_ENV:
        if os.environ.get(k):
            lines.append(f'set "{k}={os.environ[k]}"')
    lines.append(f'start "" /b "{_python(True)}" -m photointel --data "{data}" run')
    return "\r\n".join(lines) + "\r\n"


def systemd_unit(data: Path) -> str:
    env = "".join(f'Environment="{k}={os.environ[k]}"\n' for k in CARRIED_ENV if os.environ.get(k))
    return (f"[Unit]\nDescription=Memoria photo library\nAfter=network-online.target\n\n"
            f"[Service]\nWorkingDirectory={PROJECT_ROOT}\n{env}"
            f'ExecStart="{_python(False)}" -m photointel --data "{data}" run\n'
            f"Restart=on-failure\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n")


def launchd_plist(data: Path) -> str:
    args = [str(_python(False)), "-m", "photointel", "--data", str(data), "run"]
    items = "".join(f"    <string>{a}</string>\n" for a in args)
    env = "".join(f"    <key>{k}</key><string>{os.environ[k]}</string>\n" for k in CARRIED_ENV if os.environ.get(k))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f"  <key>Label</key><string>{PLIST[:-6]}</string>\n"
            f"  <key>ProgramArguments</key><array>\n{items}  </array>\n"
            f"  <key>WorkingDirectory</key><string>{PROJECT_ROOT}</string>\n"
            f"  <key>EnvironmentVariables</key><dict>\n{env}  </dict>\n"
            "  <key>RunAtLoad</key><true/>\n</dict></plist>\n")


def _systemd_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def install(data: Path, at_boot: bool = False) -> dict:
    data = Path(data).resolve()
    if os.name == "nt":
        launcher = data / "memoria-start.cmd"
        launcher.write_text(launcher_text(data), encoding="utf-8")
        if at_boot:
            r = _run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONSTART", "/RU", "SYSTEM",
                      "/RL", "HIGHEST", "/TR", f'"{launcher}"'])
            if r.returncode != 0:
                raise PermissionError(((r.stderr or r.stdout).strip() or "schtasks failed")
                                      + " — run this from an administrator terminal")
            return {"method": "boot", "where": f"Task Scheduler: {TASK_NAME}"}
        target = startup_folder() / "Memoria.cmd"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(launcher, target)
        return {"method": "sign-in", "where": str(target)}
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / PLIST
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(launchd_plist(data), encoding="utf-8")
        _run(["launchctl", "load", "-w", str(target)])
        return {"method": "sign-in", "where": str(target)}
    target = _systemd_dir() / UNIT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(systemd_unit(data), encoding="utf-8")
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", UNIT])
    return {"method": "boot" if at_boot else "sign-in", "where": str(target),
            "note": "for starting before anyone signs in: loginctl enable-linger $USER"}


def remove() -> list[str]:
    """Remove every auto-start entry this module can make. Returns what was removed."""
    gone = []
    if os.name == "nt":
        target = startup_folder() / "Memoria.cmd"
        if target.exists():
            target.unlink()
            gone.append(str(target))
        if _task_exists():
            r = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
            if r.returncode != 0:
                raise PermissionError((r.stderr or r.stdout).strip() + " — run this from an administrator terminal")
            gone.append(f"Task Scheduler: {TASK_NAME}")
        return gone
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / PLIST
        if target.exists():
            _run(["launchctl", "unload", "-w", str(target)])
            target.unlink()
            gone.append(str(target))
        return gone
    target = _systemd_dir() / UNIT
    if target.exists():
        if shutil.which("systemctl"):
            _run(["systemctl", "--user", "disable", UNIT])
        target.unlink()
        gone.append(str(target))
    return gone


def _task_exists() -> bool:
    if os.name != "nt":
        return False
    try:
        return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def status() -> dict:
    if os.name == "nt":
        signin = startup_folder() / "Memoria.cmd"
        return {"sign_in": signin.exists(), "boot": _task_exists()}
    if sys.platform == "darwin":
        return {"sign_in": (Path.home() / "Library" / "LaunchAgents" / PLIST).exists(), "boot": False}
    return {"sign_in": (_systemd_dir() / UNIT).exists(), "boot": False}
