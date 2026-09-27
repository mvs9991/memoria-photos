"""HTTPS through Tailscale: a real, trusted certificate for this computer, without a router port.

Tailscale gives each computer a private name (`pc.tail1234.ts.net`) that only your own devices
can reach, and `tailscale cert` fetches a Let's Encrypt certificate for that name. Phones then
open `https://pc.tail1234.ts.net:8443` — trusted, so the offline copy and "Add to Home
Screen" work — from home or away. Plain http on the home network keeps working beside it.

This module finds the tailscale program, reads its state, fetches the certificate into
<data>/tls/ and says when it needs renewing (Let's Encrypt certificates last 90 days; the
scheduler renews at 30 days left). It has been exercised against a stand-in tailscale program
only: the JSON fields read (`BackendState`, `Self.DNSName`, `CertDomains`) are the ones
`tailscale status --json` documents.
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path

RENEW_BEFORE_S = 30 * 86400


def cert_paths(data: Path) -> tuple[Path, Path]:
    d = Path(data) / "tls"
    return d / "cert.pem", d / "key.pem"


def cert_files(data: Path) -> tuple[Path, Path] | None:
    cert, key = cert_paths(data)
    return (cert, key) if cert.is_file() and key.is_file() else None


def cert_info(cert: Path) -> dict | None:
    """{"not_after": epoch seconds, "names": [...]} from a PEM certificate, or None."""
    try:
        decoded = ssl._ssl._test_decode_cert(str(cert))          # type: ignore[attr-defined]
    except (OSError, ssl.SSLError, ValueError):
        return None
    names = [v for k, v in decoded.get("subjectAltName", ()) if k == "DNS"]
    return {"not_after": ssl.cert_time_to_seconds(decoded["notAfter"]), "names": names}


def find_tailscale() -> Path | None:
    candidates = [os.environ.get("PHOTOINTEL_TAILSCALE"), shutil.which("tailscale")]
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(str(Path(base) / "Tailscale" / "tailscale.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def _run(exe: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = [str(exe), *args]
    if exe.suffix.lower() == ".py":                                  # the tests' stand-in
        cmd = [sys.executable, *cmd]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0     # type: ignore[attr-defined]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)


def tailscale_status(exe: Path | None = None) -> dict:
    exe = exe or find_tailscale()
    if exe is None:
        return {"installed": False}
    try:
        r = _run(exe, "status", "--json", timeout=20)
        st = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"installed": True, "running": False, "error": str(exc)}
    domains = [d.rstrip(".") for d in (st.get("CertDomains") or [])]
    name = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
    return {"installed": True, "running": st.get("BackendState") == "Running", "name": name,
            "https_ready": bool(domains), "domain": domains[0] if domains else ""}


def fetch_cert(data: Path, domain: str, exe: Path | None = None) -> dict:
    """Ask tailscale for the certificate; written beside the old one, then swapped in whole."""
    exe = exe or find_tailscale()
    if exe is None:
        raise RuntimeError("Tailscale is not installed on this computer")
    cert, key = cert_paths(data)
    cert.parent.mkdir(parents=True, exist_ok=True)
    new_cert, new_key = cert.with_suffix(".new"), key.with_suffix(".new")
    r = _run(exe, "cert", "--cert-file", str(new_cert), "--key-file", str(new_key), domain, timeout=180)
    if r.returncode != 0 or not new_cert.is_file() or not new_key.is_file():
        for p in (new_cert, new_key):
            p.unlink(missing_ok=True)
        raise RuntimeError((r.stderr or r.stdout or "tailscale cert failed").strip())
    info = cert_info(new_cert)
    if info is None:
        raise RuntimeError("tailscale wrote a certificate that could not be read")
    os.replace(new_key, key)
    os.replace(new_cert, cert)
    return info


def renew_due(data: Path, now: float | None = None) -> bool:
    files = cert_files(data)
    info = cert_info(files[0]) if files else None
    return bool(info) and info["not_after"] - (now or time.time()) < RENEW_BEFORE_S


def https_url(settings, info: dict | None) -> str:
    if not info or not info["names"]:
        return ""
    port = settings.https_port
    return f"https://{info['names'][0]}" + ("" if port == 443 else f":{port}")
