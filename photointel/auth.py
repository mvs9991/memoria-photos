"""Optional password protection and read-only share links.

Memoria has no accounts: it is one person's library. By default it listens only on this
machine and needs no login. Setting a password makes every API route require a session,
which is what makes it safe to open to a phone on the home network (`serve --host
0.0.0.0` refuses to start without one). Share links give someone else read-only access
to exactly one album and nothing else.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

SESSION_COOKIE = "memoria_session"
SESSION_DAYS = 30
PBKDF2_ROUNDS = 240_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def _secret(data_dir: Path) -> bytes:
    path = Path(data_dir) / "secret.key"
    if not path.exists():
        path.write_bytes(os.urandom(32))
    return path.read_bytes()


def make_session(data_dir: Path) -> str:
    issued = str(int(time.time()))
    sig = hmac.new(_secret(data_dir), issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


def valid_session(data_dir: Path, token: str | None) -> bool:
    """A signed, unexpired session issued after the last password change (so changing the
    password logs every other browser out)."""
    if not token or "." not in token:
        return False
    issued, sig = token.split(".", 1)
    good = hmac.new(_secret(data_dir), issued.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        age = time.time() - int(issued)
    except ValueError:
        return False
    changed = float(_password_changed_at(data_dir))
    return 0 <= age <= SESSION_DAYS * 86400 and int(issued) >= int(changed)


def _password_changed_at(data_dir: Path) -> float:
    path = Path(data_dir) / "password.changed"
    return float(path.read_text()) if path.exists() else 0.0


def mark_password_changed(data_dir: Path) -> None:
    (Path(data_dir) / "password.changed").write_text(str(int(time.time())))


# ---------------------------------------------------------------- share links

def create_share(conn: sqlite3.Connection, album_id: int, allow_download: bool = False,
                 expires_days: float | None = None) -> dict:
    token = secrets.token_urlsafe(16)
    now = time.time()
    expires = now + expires_days * 86400 if expires_days else None
    conn.execute("INSERT INTO share_links(token, album_id, allow_download, expires_at, created_at) VALUES (?,?,?,?,?)",
                 (token, album_id, int(allow_download), expires, now))
    conn.commit()
    return {"token": token, "album_id": album_id, "allow_download": allow_download, "expires_at": expires}


def resolve_share(conn: sqlite3.Connection, token: str):
    row = conn.execute("SELECT s.*, a.name, a.kind, a.query, a.hidden FROM share_links s "
                       "JOIN albums a ON a.id = s.album_id WHERE s.token = ?", (token,)).fetchone()
    if row is None or row["hidden"]:
        return None
    if row["expires_at"] and row["expires_at"] < time.time():
        return None
    conn.execute("UPDATE share_links SET last_used_at = ? WHERE token = ?", (time.time(), token))
    conn.commit()
    return row


def revoke_share(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM share_links WHERE token = ?", (token,))
    conn.commit()


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


# ---------------------------------------------------------------- signed short-lived tokens

LOCKED_COOKIE = "memoria_locked"
LOCKED_MINUTES = 15


def sign(data_dir: Path, message: str) -> str:
    return hmac.new(_secret(data_dir), message.encode(), hashlib.sha256).hexdigest()


def make_locked_token(data_dir: Path) -> str:
    """Opens the Locked folder for LOCKED_MINUTES in this browser only."""
    msg = f"locked:{int(time.time() * 1000)}"
    return f"{msg}.{sign(data_dir, msg)}"


def valid_locked_token(data_dir: Path, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    msg, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, sign(data_dir, msg)) or not msg.startswith("locked:"):
        return False
    try:
        issued_ms = int(msg.split(":", 1)[1])
    except ValueError:
        return False
    # Milliseconds: a token opened in the same second as a PIN change must still be refused.
    return (0 <= time.time() * 1000 - issued_ms <= LOCKED_MINUTES * 60_000
            and issued_ms > _pin_changed_at(data_dir) * 1000)


def _pin_changed_at(data_dir: Path) -> float:
    path = Path(data_dir) / "pin.changed"
    return float(path.read_text()) if path.exists() else 0.0


def mark_pin_changed(data_dir: Path) -> None:
    (Path(data_dir) / "pin.changed").write_text(repr(time.time()))


# ---------------------------------------------------------------- account sessions

def make_user_session(data_dir: Path, user_id: int) -> str:
    msg = f"u{user_id}:{int(time.time() * 1000)}"
    return f"{msg}.{sign(data_dir, msg)}"


def session_user_id(data_dir: Path, token: str | None) -> tuple[int, float] | None:
    """-> (account id, issued at in seconds, to the millisecond) for a signed, unexpired
    session. The caller compares it with that account's own password-change time."""
    if not token or not token.startswith("u") or "." not in token:
        return None
    msg, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, sign(data_dir, msg)):
        return None
    try:
        uid_s, issued_s = msg[1:].split(":", 1)
        uid, issued = int(uid_s), int(issued_s) / 1000
    except ValueError:
        return None
    if not (0 <= time.time() - issued <= SESSION_DAYS * 86400):
        return None
    return uid, issued
