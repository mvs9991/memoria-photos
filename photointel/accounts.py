"""Family accounts: one shared library, several people with their own logins.

Roles (enforced for every API request in api/app.py, by `allowed()`):
* owner  — everything: settings, folders, the Trash, backup, the Locked folder, accounts.
* family — browse, search, upload, favourites, ratings, albums, tags, people, corrections,
           share links and .zip downloads. Nothing that deletes, changes settings, writes
           to the server's disks, or opens the Locked folder.
* guest  — look and download: read-only requests, plus .zip downloads.

Accounts are optional. Without any, the library is one person's and the single password
(if set) is theirs. Turning accounts on makes that password the owner account's.
"""
from __future__ import annotations

import re
import sqlite3
import time

from . import auth, db

ROLES = ("owner", "family", "guest")
_NAME = re.compile(r"^[\w .@-]{2,40}$", re.U)

# Paths only an owner may use at all, and ones only an owner may change.
OWNER_ONLY = ("/api/trash", "/api/locked", "/api/accounts", "/api/roots", "/api/cache", "/api/backup",
              "/api/export/xmp", "/api/gpx", "/api/photos/lock", "/api/photos/unlock", "/api/auth/password",
              "/api/audit", "/api/errors", "/api/browse")
OWNER_ONLY_WRITES = ("/api/settings", "/api/jobs")
OPEN_TO_ALL = ("/api/accounts/me",)
GUEST_POSTS = ("/api/export/zip", "/api/export/preview")


def _ms_now() -> float:
    """Seconds, truncated to the millisecond — the precision session cookies carry."""
    return int(time.time() * 1000) / 1000


class AccountError(ValueError):
    pass


def enabled(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def allowed(role: str, method: str, path: str) -> bool:
    if role == "owner":
        return True
    if path.startswith(OPEN_TO_ALL):
        return True
    if path.startswith(OWNER_ONLY) or path.endswith("/export-html") or path == "/api/export":
        return False
    if method not in ("GET", "HEAD") and path.startswith(OWNER_ONLY_WRITES):
        return False
    if role == "guest":
        return method in ("GET", "HEAD") or path.startswith(GUEST_POSTS)
    return role == "family"


def _check(username: str, password: str | None, role: str | None) -> None:
    if not _NAME.match(username or ""):
        raise AccountError("a name of 2 to 40 letters, digits, spaces or . @ - _")
    if password is not None and len(password) < 6:
        raise AccountError("use a password of at least 6 characters")
    if role is not None and role not in ROLES:
        raise AccountError(f"role must be one of {', '.join(ROLES)}")


def enable(ctx, conn: sqlite3.Connection, owner_name: str, owner_password: str | None = None) -> dict:
    """Turn accounts on. The owner keeps the library's current password unless a new one is given."""
    if enabled(conn):
        raise AccountError("accounts are already on")
    _check(owner_name, owner_password, "owner")
    stored = auth.hash_password(owner_password) if owner_password else ctx.settings.access_password_hash
    if not stored:
        raise AccountError("set a password for yourself first")
    now = _ms_now()
    uid = conn.execute("INSERT INTO users(username, password_hash, role, created_at, pw_changed_at) VALUES (?,?,?,?,?)",
                       (owner_name.strip(), stored, "owner", now, now)).lastrowid
    db.audit(conn, "accounts_enabled", "user", uid, {"owner": owner_name})
    conn.commit()
    return get(conn, uid)


def create(conn: sqlite3.Connection, username: str, password: str, role: str = "family") -> dict:
    _check(username, password, role)
    try:
        # pw_changed_at = now: a session cookie issued before this account existed (for a deleted
        # account whose id SQLite may hand out again) can never sign in as it.
        now = _ms_now()
        uid = conn.execute("INSERT INTO users(username, password_hash, role, created_at, pw_changed_at) "
                           "VALUES (?,?,?,?,?)", (username.strip(), auth.hash_password(password), role, now, now)).lastrowid
    except sqlite3.IntegrityError:
        raise AccountError("that name is taken")
    db.audit(conn, "account_created", "user", uid, {"username": username, "role": role})
    conn.commit()
    return get(conn, uid)


def update(conn: sqlite3.Connection, uid: int, acting_uid: int | None, role: str | None = None,
           password: str | None = None, disabled: bool | None = None) -> dict:
    u = get(conn, uid)
    if u is None:
        raise AccountError("no such account")
    _check(u["username"], password, role)
    owners = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'owner' AND disabled = 0").fetchone()[0]
    losing_owner = u["role"] == "owner" and ((role and role != "owner") or disabled)
    if losing_owner and owners <= 1:
        raise AccountError("the library needs at least one owner")
    if uid == acting_uid and disabled:
        raise AccountError("you cannot turn off your own account")
    if role:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    if password:        # and every session of that account ends
        conn.execute("UPDATE users SET password_hash = ?, pw_changed_at = ? WHERE id = ?",
                     (auth.hash_password(password), _ms_now(), uid))
    if disabled is not None:
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), uid))
    db.audit(conn, "account_changed", "user", uid, {"role": role, "password": bool(password), "disabled": disabled})
    conn.commit()
    return get(conn, uid)


def delete(conn: sqlite3.Connection, uid: int, acting_uid: int | None) -> None:
    u = get(conn, uid)
    if u is None:
        return
    if uid == acting_uid:
        raise AccountError("you cannot remove your own account")
    if u["role"] == "owner" and conn.execute("SELECT COUNT(*) FROM users WHERE role = 'owner'").fetchone()[0] <= 1:
        raise AccountError("the library needs at least one owner")
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.audit(conn, "account_removed", "user", uid, {"username": u["username"]})
    conn.commit()


def get(conn: sqlite3.Connection, uid: int) -> dict | None:
    r = conn.execute("SELECT id, username, role, disabled, created_at, last_login_at FROM users WHERE id = ?",
                     (uid,)).fetchone()
    return dict(r) if r else None


def from_session(ctx, conn: sqlite3.Connection, token: str | None) -> dict | None:
    """The signed-in, enabled account for a session cookie, or None."""
    got = auth.session_user_id(ctx.paths.data, token)
    if got is None:
        return None
    uid, issued = got
    r = conn.execute("SELECT id, username, role, disabled, pw_changed_at FROM users WHERE id = ?", (uid,)).fetchone()
    # Both times are whole milliseconds: a session from before the account (re)started is refused,
    # one issued in the same millisecond as the change (the one handed out with it) is not.
    if r is None or r["disabled"] or issued < (r["pw_changed_at"] or 0):
        return None
    return {"id": r["id"], "username": r["username"], "role": r["role"]}


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, username, role, disabled, created_at, last_login_at FROM users "
        "ORDER BY role != 'owner', username")]


def check_login(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    r = conn.execute("SELECT * FROM users WHERE username = ? AND disabled = 0", ((username or "").strip(),)).fetchone()
    if r is None or not auth.verify_password(password, r["password_hash"]):
        return None
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), r["id"]))
    conn.commit()
    return get(conn, int(r["id"]))
