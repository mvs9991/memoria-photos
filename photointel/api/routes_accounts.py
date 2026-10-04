"""Family accounts, the Locked folder and the Archive.

Who may call what is decided in api/app.py (accounts.allowed): everything here except
/accounts/me is the owner's, and the Locked folder only opens for an owner.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import accounts, auth, db, ratelimit
from ..engine import locked as locked_mod
from ..engine import visibility
from .deps import current_user, get_state, locked_open
from .routes_library import columnar

router = APIRouter()


def _me_id() -> int | None:
    conn = get_state().conn()
    name = current_user()
    if not name:
        return None
    r = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()
    return int(r[0]) if r else None


def _err(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------- accounts

@router.get("/accounts")
def list_accounts():
    conn = get_state().conn()
    return {"enabled": accounts.enabled(conn), "accounts": accounts.list_all(conn)}


class EnableBody(BaseModel):
    username: str
    password: str | None = None


@router.post("/accounts/enable")
def enable(body: EnableBody, response: Response):
    """Turn accounts on; the caller becomes the owner and stays signed in."""
    state = get_state()
    user = _err(accounts.enable, state.ctx, state.conn(), body.username, body.password)
    response.set_cookie(auth.SESSION_COOKIE, auth.make_user_session(state.ctx.paths.data, user["id"]),
                        httponly=True, samesite="lax", max_age=auth.SESSION_DAYS * 86400)
    return user


class NewAccount(BaseModel):
    username: str
    password: str
    role: str = "family"


@router.post("/accounts")
def create_account(body: NewAccount):
    conn = get_state().conn()
    if not accounts.enabled(conn):
        raise HTTPException(400, "turn accounts on first")
    return _err(accounts.create, conn, body.username, body.password, body.role)


class AccountChange(BaseModel):
    role: str | None = None
    password: str | None = None
    disabled: bool | None = None


@router.post("/accounts/{uid}")
def change_account(uid: int, body: AccountChange):
    return _err(accounts.update, get_state().conn(), uid, _me_id(), body.role, body.password, body.disabled)


@router.delete("/accounts/{uid}")
def remove_account(uid: int):
    _err(accounts.delete, get_state().conn(), uid, _me_id())
    return {"ok": True}


@router.get("/accounts/me")
def me():
    uid = _me_id()
    conn = get_state().conn()
    return accounts.get(conn, uid) if uid else {"id": None, "username": None, "role": "owner"}


class MyPassword(BaseModel):
    current: str
    new: str


@router.post("/accounts/me/password")
def my_password(body: MyPassword, response: Response):
    state = get_state()
    conn = state.conn()
    uid = _me_id()
    if uid is None:
        raise HTTPException(400, "accounts are off; set the library password in Settings → Access")
    name = current_user() or ""
    keys = (f"user:{name.lower()}",)
    wait = ratelimit.LOGINS.wait_for(*keys)
    if wait > 0:
        raise HTTPException(429, ratelimit.refuse_message(wait))
    if accounts.check_login(conn, name, body.current) is None:
        ratelimit.LOGINS.fail(*keys)
        raise HTTPException(401, "current password is wrong")
    user = _err(accounts.update, conn, uid, uid, password=body.new)
    response.set_cookie(auth.SESSION_COOKIE, auth.make_user_session(state.ctx.paths.data, uid), httponly=True,
                        samesite="lax", max_age=auth.SESSION_DAYS * 86400)
    return user


# ---------------------------------------------------------------- the Locked folder

@router.get("/locked/status")
def locked_status():
    state = get_state()
    n = state.conn().execute("SELECT COUNT(*) FROM photos WHERE status = 'locked' AND live_component = 0").fetchone()[0]
    return {"pin_set": bool(state.ctx.settings.locked_pin_hash), "open": locked_open(), "count": n}


class PinBody(BaseModel):
    current: str | None = None
    new: str


def _pin_limit() -> None:
    wait = ratelimit.PINS.wait_for("pin")
    if wait > 0:
        raise HTTPException(429, ratelimit.refuse_message(wait))


@router.post("/locked/pin")
def set_pin(body: PinBody, response: Response):
    state = get_state()
    s = state.ctx.settings
    if s.locked_pin_hash:
        _pin_limit()
        if not auth.verify_password(body.current or "", s.locked_pin_hash):
            ratelimit.PINS.fail("pin")          # one counter for every device: a PIN has few values
            raise HTTPException(401, "current PIN is wrong")
    if len(body.new) < 4:
        raise HTTPException(400, "use at least 4 digits or characters")
    s.locked_pin_hash = auth.hash_password(body.new)
    s.save(state.ctx.paths.data)
    auth.mark_pin_changed(state.ctx.paths.data)
    response.delete_cookie(auth.LOCKED_COOKIE)
    db.audit(state.conn(), "locked_pin_set", "settings", None, {})
    state.conn().commit()
    return {"pin_set": True}


class OpenBody(BaseModel):
    pin: str


@router.post("/locked/open")
def open_locked(body: OpenBody, response: Response):
    state = get_state()
    stored = state.ctx.settings.locked_pin_hash
    if not stored:
        raise HTTPException(400, "set a PIN first")
    _pin_limit()
    if not auth.verify_password(body.pin, stored):
        ratelimit.PINS.fail("pin")
        raise HTTPException(401, "wrong PIN")
    ratelimit.PINS.succeed("pin")
    response.set_cookie(auth.LOCKED_COOKIE, auth.make_locked_token(state.ctx.paths.data), httponly=True,
                        samesite="strict", max_age=auth.LOCKED_MINUTES * 60)
    return {"open": True, "minutes": auth.LOCKED_MINUTES}


@router.post("/locked/close")
def close_locked(request: Request, response: Response):
    auth.revoke_locked_token(get_state().ctx.paths.data, request.cookies.get(auth.LOCKED_COOKIE))
    response.delete_cookie(auth.LOCKED_COOKIE)
    return {"open": False}


@router.get("/locked/photos")
def locked_photos():
    if not locked_open():
        raise HTTPException(403, "the Locked folder is closed")
    rows = get_state().conn().execute(
        "SELECT p.id, p.width, p.height, p.rotation, p.taken_ts, p.face_count, " + _fav() + " AS favorite, p.media_type, p.duration, "
        "p.live_video_id, p.motion_offset, p.rating FROM photos p WHERE p.status = 'locked' AND p.live_component = 0 "
        "ORDER BY p.taken_ts DESC, p.id DESC").fetchall()
    return columnar(rows)


class IdsBody(BaseModel):
    photo_ids: list[int]


@router.post("/photos/lock")
def lock_photos(body: IdsBody):
    state = get_state()
    if not state.ctx.settings.locked_pin_hash:
        raise HTTPException(400, "set a PIN for the Locked folder first")
    return {"locked": locked_mod.lock(state.conn(), body.photo_ids)}


@router.post("/photos/unlock")
def unlock_photos(body: IdsBody):
    if not locked_open():
        raise HTTPException(403, "open the Locked folder first")
    return {"unlocked": locked_mod.unlock(get_state().conn(), body.photo_ids)}


# ---------------------------------------------------------------- the Archive

class ArchiveBody(BaseModel):
    photo_ids: list[int]
    archived: bool = True


@router.post("/photos/archive")
def archive(body: ArchiveBody):
    """Out of the timeline, memories and the photo frame; still in search, albums and people."""
    conn = get_state().conn()
    ids = visibility.companions(conn, body.photo_ids)
    if not ids:
        return {"changed": 0}
    n = sum(conn.execute(f"UPDATE photos SET archived = ? WHERE id IN ({marks})", (int(body.archived), *chunk)).rowcount
            for chunk, marks in db.chunks(ids))
    db.audit(conn, "photos_archived" if body.archived else "photos_unarchived", "photo", None, {"photos": ids[:2000]})
    conn.commit()
    visibility.refresh(conn, ids)
    return {"changed": n}


def _fav() -> str:
    from ..engine.favorites import expr
    from .deps import current_user_id

    return expr("p", current_user_id())


# ---------------------------------------------------------------- private photos (engine/private.py)

def _account() -> dict:
    uid = _me_id()
    if uid is None:
        raise HTTPException(400, "private photos need accounts (Settings > People who can sign in)")
    user = accounts.get(get_state().conn(), uid)
    if user is None:
        raise HTTPException(404, "no such account")
    return user


@router.get("/private/photos")
def private_photos():
    uid = _me_id()
    if uid is None:
        return columnar([])
    rows = get_state().conn().execute(
        "SELECT p.id, p.width, p.height, p.rotation, p.taken_ts, p.face_count, " + _fav() + " AS favorite, p.media_type, "
        "p.duration, p.live_video_id, p.motion_offset, p.rating FROM photos p WHERE p.status = 'private' "
        "AND p.private_to = ? AND p.live_component = 0 ORDER BY p.taken_ts DESC, p.id DESC", (uid,)).fetchall()
    return columnar(rows)


class MaybeIdsBody(BaseModel):
    photo_ids: list[int] | None = None       # None: everything this person uploaded


@router.post("/photos/private")
def make_private(body: MaybeIdsBody):
    from ..engine import private

    state = get_state()
    return private.make_private(state.ctx, state.conn(), _account(), body.photo_ids)


@router.post("/photos/share-with-family")
def share_with_family(body: IdsBody):
    from ..engine import private

    return {"shared": private.share(get_state().conn(), _account(), body.photo_ids)}


class OnBody(BaseModel):
    on: bool


@router.post("/accounts/me/private-uploads")
def private_uploads(body: OnBody):
    user = _account()
    conn = get_state().conn()
    conn.execute("UPDATE users SET private_uploads = ? WHERE id = ?", (int(body.on), user["id"]))
    conn.commit()
    return {"private_uploads": body.on}
