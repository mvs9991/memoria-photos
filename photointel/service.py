"""Keeping Memoria running: a small keeper process, and asking it for a restart.

`python -m photointel run` is what auto-start launches. It starts the server as a child
process and starts it again when it stops unexpectedly (a crash, a killed process), waiting
a little longer each time it keeps failing. It does *not* restart:
  * after a clean stop (exit 0: Ctrl+C, or the computer shutting down), and
  * after a configuration refusal (exit 2: e.g. serving to the network without a password),
    because starting again would only fail again; the reason is in the log and in the app.
The server asks for a restart (new certificate, changed address) by exiting with
RESTART_CODE, which the keeper answers at once.

What happened is kept in <data>/service.json (for the app's Settings and the health checks).
The child's own output goes to <data>/logs/server-output.log, so a crash leaves its traceback.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

RESTART_CODE = 75            # "restart me" (EX_TEMPFAIL)
CONFIG_ERROR = 2             # argparse and cmd_serve's refusals
BACKOFF = (3, 10, 30, 60, 300)
HEALTHY_AFTER_S = 600        # a child that ran this long resets the backoff
SUPERVISED_ENV = "MEMORIA_SUPERVISED"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_LOG_MAX = 5 << 20


def state_path(data: Path) -> Path:
    return Path(data) / "service.json"


def read_state(data: Path) -> dict:
    try:
        return json.loads(state_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(data: Path, state: dict) -> None:
    p = state_path(data)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def supervised() -> bool:
    """Whether this server was started by the keeper (so a restart request will be answered)."""
    return os.environ.get(SUPERVISED_ENV) == "1"


def request_restart(delay: float = 0.5) -> None:
    """Exit with RESTART_CODE shortly (after the HTTP answer has gone out). Only when supervised."""
    if not supervised():
        raise RuntimeError("not started by `photointel run`")
    threading.Timer(delay, lambda: os._exit(RESTART_CODE)).start()


def child_command(data: Path, serve_args: list[str]) -> list[str]:
    exe = Path(sys.executable)
    console = exe.with_name("python.exe" if os.name == "nt" else exe.name)   # pythonw has no output to keep
    return [str(console if console.exists() else exe), "-m", "photointel", "--data", str(data), "serve", *serve_args]


def next_delay(failures: int) -> float:
    return BACKOFF[min(failures, len(BACKOFF)) - 1] if failures > 0 else 0


def decide(code: int, ran_for: float, failures: int) -> tuple[str, int]:
    """-> (what to do, failures so far). Pure, so the rules are tested without processes."""
    if code == 0:
        return "stop", 0
    if code == CONFIG_ERROR:
        return "give-up", failures
    if code == RESTART_CODE:
        return "restart", 0
    failures = 1 if ran_for >= HEALTHY_AFTER_S else failures + 1
    return "restart-later", failures


def _open_output(logs: Path):
    logs.mkdir(parents=True, exist_ok=True)
    out = logs / "server-output.log"
    try:
        if out.stat().st_size > OUTPUT_LOG_MAX:
            out.replace(out.with_suffix(".log.1"))
    except OSError:
        pass
    return open(out, "ab")


def run(data: Path, serve_args: list[str], stop: threading.Event | None = None,
        spawn=None, sleep=time.sleep) -> str:
    """The keeper loop. Returns why it ended ("stopped", "config-error", "interrupted")."""
    data = Path(data)
    stop = stop or threading.Event()
    spawn = spawn or _spawn
    state = read_state(data)
    state.update({"keeper_pid": os.getpid(), "keeper_started_at": time.time(), "status": "running"})
    state.setdefault("restarts", [])
    failures = 0
    try:
        while not stop.is_set():
            started = time.time()
            state.update({"child_started_at": started, "status": "running"})
            _write_state(data, state)
            code = spawn(child_command(data, serve_args), data)
            ran = time.time() - started
            action, failures = decide(code, ran, failures)
            state.update({"last_exit": code, "last_exit_at": time.time()})
            if action == "stop" or stop.is_set():
                state["status"] = "stopped"
                _write_state(data, state)
                return "stopped"
            if action == "give-up":
                state["status"] = "config-error"
                _write_state(data, state)
                log.error("The server refused to start (exit %s); not retrying. See logs/server-output.log", code)
                return "config-error"
            if action == "restart-later":
                state["restarts"] = [t for t in state["restarts"] if t > time.time() - 7 * 86400][-50:] + [time.time()]
                wait = next_delay(failures)
                log.warning("The server stopped unexpectedly (exit %s); starting it again in %ss", code, wait)
                _write_state(data, state)
                sleep(wait)
            else:
                log.info("Restarting the server as it asked")
    except KeyboardInterrupt:
        state["status"] = "stopped"
        _write_state(data, state)
        return "interrupted"
    return "stopped"


def _spawn(cmd: list[str], data: Path) -> int:
    env = dict(os.environ, **{SUPERVISED_ENV: "1"})
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0          # type: ignore[attr-defined]
    with _open_output(data / "logs") as out:
        out.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {' '.join(cmd[1:])}\n".encode())
        out.flush()
        proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
                                creationflags=flags)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise


def find_processes(data: Path) -> list[tuple[int, str, str]]:
    """(pid, role, command line) of Memoria's keeper ("run") and server ("serve") processes for this library."""
    want = os.path.normcase(str(Path(data).resolve()))
    found = []
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=60).stdout
        lines = [ln.split("	", 1) for ln in out.splitlines() if "	" in ln]
    else:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=60).stdout
        lines = [ln.strip().split(" ", 1) for ln in out.splitlines() if ln.strip()]
    for pid, cmd in lines:
        if "photointel" not in cmd or int(pid) == os.getpid():
            continue
        role = _subcommand(cmd)
        if role in ("run", "serve") and want in os.path.normcase(cmd.replace('"', "")):
            found.append((int(pid), role, cmd))
    return found


def _subcommand(cmd: str) -> str | None:
    """The photointel subcommand in a command line: the first word after "photointel" that is not an option
    or an option's value (`python -m photointel --data D:/lib run --host 0.0.0.0` -> "run")."""
    parts = cmd.replace('"', " ").split()
    try:
        i = parts.index("photointel") + 1
    except ValueError:
        return None
    while i < len(parts):
        if parts[i] in ("--data",):
            i += 2
        elif parts[i].startswith("-"):
            i += 1
        else:
            return parts[i]
    return None


def stop(data: Path) -> list[int]:
    """Stop the keeper first (or it would start the server again), then the server. Returns the pids stopped."""
    procs = find_processes(data)
    stopped = []
    for role in ("run", "serve"):
        for pid, r, _ in procs:
            if r != role:
                continue
            if os.name == "nt":
                ok = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True).returncode == 0
            else:
                import signal

                try:
                    os.kill(pid, signal.SIGTERM)
                    ok = True
                except OSError:
                    ok = False
            if ok:
                stopped.append(pid)
    state = read_state(data)
    if stopped:
        state["status"] = "stopped"
        _write_state(data, state)
    return stopped


def restarts_since(data: Path, since: float) -> int:
    return sum(1 for t in read_state(data).get("restarts", []) if t >= since)


# ---------------------------------------------------------------- keeping the computer awake

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_awake = None


def keep_awake(on: bool) -> bool:
    """Windows: stop the computer going to sleep while this process runs (the screen may still
    turn off). Called from one long-lived thread (the scheduler's): the request belongs to the
    thread that made it. Elsewhere a no-op returning False."""
    global _awake
    if os.name != "nt" or _awake == on:
        return os.name == "nt"
    import ctypes

    flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
    ok = bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))      # type: ignore[attr-defined]
    if ok:
        _awake = on
    return ok
