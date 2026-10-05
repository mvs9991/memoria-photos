"""Login (when a password is set) and read-only share links."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from .. import accounts, auth, ratelimit
from ..engine import albums as albums_mod
from .deps import get_state
from .routes_library import columnar

router = APIRouter()


@router.get("/auth/status")
def status(request: Request):
    state = get_state()
    ctx, conn = state.ctx, state.conn()
    token = request.cookies.get(auth.SESSION_COOKIE)
    if accounts.enabled(conn):
        user = accounts.from_session(ctx, conn, token)
        return {"protected": True, "accounts": True, "logged_in": user is not None, "user": user}
    protected = bool(ctx.settings.access_password_hash)
    logged_in = (not protected) or auth.valid_session(ctx.paths.data, token)
    return {"protected": protected, "accounts": False, "logged_in": logged_in,
            "user": {"id": None, "username": None, "role": "owner"} if logged_in else None}


class LoginBody(BaseModel):
    username: str | None = None
    password: str


def _login_keys(request: Request, name: str | None) -> tuple[str, ...]:
    host = request.client.host if request.client else "?"
    who = (name or "").strip().lower()
    if not who:
        # The single-password library has no account name. A shared "user:" key would make ten wrong
        # guesses from any one device (or any stranger on the network) lock the owner out of their own
        # library for 15 minutes, so there the address alone is what is limited.
        return (f"ip:{host}",)
    return (f"ip:{host}", f"user:{who}")


def _check_limit(limiter, keys) -> None:
    wait = limiter.wait_for(*keys)
    if wait > 0:
        raise HTTPException(429, ratelimit.refuse_message(wait))


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    state = get_state()
    ctx, conn = state.ctx, state.conn()
    keys = _login_keys(request, body.username)
    _check_limit(ratelimit.LOGINS, keys)
    if accounts.enabled(conn):
        user = accounts.check_login(conn, body.username or "", body.password)
        if user is None:
            ratelimit.LOGINS.fail(*keys)
            raise HTTPException(401, "wrong name or password")
        ratelimit.LOGINS.succeed(*keys)
        response.set_cookie(auth.SESSION_COOKIE, auth.make_user_session(ctx.paths.data, user["id"]), httponly=True,
                            samesite="lax", max_age=auth.SESSION_DAYS * 86400)
        return {"ok": True, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}}
    if not ctx.settings.access_password_hash:
        return {"ok": True}
    if not auth.verify_password(body.password, ctx.settings.access_password_hash):
        ratelimit.LOGINS.fail(*keys)
        raise HTTPException(401, "wrong password")
    ratelimit.LOGINS.succeed(*keys)
    response.set_cookie(auth.SESSION_COOKIE, auth.make_session(ctx.paths.data), httponly=True, samesite="lax",
                        max_age=auth.SESSION_DAYS * 86400)
    return {"ok": True}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE)
    response.delete_cookie(auth.LOCKED_COOKIE)
    return {"ok": True}


class PasswordBody(BaseModel):
    current: str | None = None
    new: str


@router.post("/auth/password")
def set_password(body: PasswordBody, request: Request, response: Response):
    """Set, change or (with an empty new password) remove the app password."""
    ctx = get_state().ctx
    if accounts.enabled(get_state().conn()):
        raise HTTPException(400, "accounts are on: change passwords in Settings → Accounts")
    stored = ctx.settings.access_password_hash
    keys = _login_keys(request, "(library password)")
    _check_limit(ratelimit.LOGINS, keys)
    if stored and not auth.verify_password(body.current or "", stored):
        ratelimit.LOGINS.fail(*keys)
        raise HTTPException(401, "current password is wrong")
    if body.new and len(body.new) < 6:
        raise HTTPException(400, "use at least 6 characters")
    ctx.settings.access_password_hash = auth.hash_password(body.new) if body.new else ""
    ctx.settings.save(ctx.paths.data)
    auth.mark_password_changed(ctx.paths.data)
    if body.new:   # stay logged in on the browser that set it
        response.set_cookie(auth.SESSION_COOKIE, auth.make_session(ctx.paths.data), httponly=True,
                            samesite="lax", max_age=auth.SESSION_DAYS * 86400)
    return {"protected": bool(body.new)}


# ---------------------------------------------------------------- share links (owner side)

class ShareBody(BaseModel):
    allow_download: bool = False
    expires_days: float | None = None
    allow_upload: bool = False


@router.post("/albums/{album_id}/share")
def share_album(album_id: int, body: ShareBody):
    conn = get_state().conn()
    from .deps import current_user_id

    album = albums_mod.can_see(conn, album_id, current_user_id())
    if album is None:
        raise HTTPException(404, "album not found")
    if body.allow_upload and album["kind"] != "manual":
        raise HTTPException(400, "a smart album is a saved search; people cannot add photos to it")
    return auth.create_share(conn, album_id, body.allow_download, body.expires_days, body.allow_upload)


def _own_album(conn, album_id: int) -> None:
    """Share links belong to the album: only an account that may see the album may list or revoke them
    (another owner's private album's live tokens were listed to anyone who asked)."""
    from .deps import current_user_id

    if albums_mod.can_see(conn, album_id, current_user_id()) is None:
        raise HTTPException(404, "album not found")


@router.get("/albums/{album_id}/shares")
def list_shares(album_id: int):
    _own_album(get_state().conn(), album_id)
    rows = get_state().conn().execute(
        "SELECT token, allow_download, allow_upload, expires_at, created_at, last_used_at FROM share_links WHERE album_id = ? "
        "ORDER BY created_at DESC", (album_id,)).fetchall()
    return {"shares": [dict(r) for r in rows]}


@router.delete("/shares/{token}")
def revoke(token: str):
    conn = get_state().conn()
    row = conn.execute("SELECT album_id FROM share_links WHERE token = ?", (token,)).fetchone()
    if row is not None:
        _own_album(conn, int(row["album_id"]))
        auth.revoke_share(conn, token)
    return {"ok": True}


# ---------------------------------------------------------------- share links (visitor side)

_SHARE_IDS_TTL = 30.0
_share_ids_cache: dict[str, tuple[float, list[int], frozenset]] = {}


def _share_photo_ids(conn, share) -> list[int]:
    """The shared album's photos, remembered for half a minute per link. Every thumbnail, video and download
    through a link checks membership against this list, and for a smart album it is a full search: a visitor
    opening a 100-photo album ran 100 searches. (Revoking or expiry still apply at once: `_require` resolves
    the link on every request, before this is used.)"""
    key = str(share["token"]) if "token" in share.keys() else f"album:{share['album_id']}"
    hit = _share_ids_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _SHARE_IDS_TTL:
        return hit[1]
    if share["kind"] == "smart":
        ids = _search_as_album_owner(conn, share)
    else:
        ids = albums_mod.album_photo_ids(conn, share["album_id"])
    if len(_share_ids_cache) > 256:
        _share_ids_cache.clear()
    _share_ids_cache[key] = (now, ids, frozenset(ids))
    return ids


def _search_as_album_owner(conn, share) -> list[int]:
    """A shared smart album's search, run as the account that made the album. A visitor has no account, and
    with none the search saw every album, private ones too: "album Therapy" shared another person's private
    album to anyone holding the link."""
    from .deps import set_current_user, _current_user

    user = None
    if accounts.enabled(conn):
        owner = share["owner_user_id"] if "owner_user_id" in share.keys() else None
        user = (accounts.get(conn, int(owner)) if owner else None) or {"id": -1, "username": None, "role": "guest"}
    token = set_current_user(user)
    try:
        return get_state().search.search(conn, share["query"], limit=5000).photo_ids
    finally:
        _current_user.reset(token)


def _in_share(conn, share, photo_id: int) -> bool:
    _share_photo_ids(conn, share)
    key = str(share["token"]) if "token" in share.keys() else f"album:{share['album_id']}"
    return photo_id in _share_ids_cache[key][2]


def _require(token: str):
    conn = get_state().conn()
    share = auth.resolve_share(conn, token)
    if share is None:
        raise HTTPException(404, "this link has expired or was revoked")
    return conn, share


@router.get("/share/{token}")
def shared_album(token: str):
    conn, share = _require(token)
    ids = _share_photo_ids(conn, share)
    rows = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        rows += conn.execute(
            f"SELECT id, width, height, rotation, taken_ts, face_count, favorite, media_type, duration, live_video_id, "
            f"motion_offset FROM photos WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    order = {pid: n for n, pid in enumerate(ids)}
    rows.sort(key=lambda r: order[r["id"]])
    payload = columnar(rows)
    payload["flags"] = [f & ~1 for f in payload["flags"]]   # the owner's favourites are not shared
    return {"name": share["name"], "allow_download": bool(share["allow_download"]),
            "allow_upload": bool(share["allow_upload"]), "photos": payload}


@router.post("/share/{token}/upload")
def shared_upload(token: str, files: list[UploadFile]):
    """A visitor adds photos to a shared album whose link allows it. They are stored in the
    upload folder under Shared/<album>, and join the album once indexed."""
    from ..engine.uploads import save_upload

    conn, share = _require(token)
    if not share["allow_upload"]:
        raise HTTPException(403, "this link does not allow adding photos")
    ctx = get_state().ctx
    out = [save_upload(ctx, conn, f.file, f.filename or "upload", who="Shared", subfolder=share["name"],
                       album_id=int(share["album_id"])).__dict__ for f in files[:200]]
    for r in out:
        r.pop("path", None)                  # the visitor learns nothing about the server's folders
        r.pop("photo_id", None)              # nor which of the owner's photos their bytes matched
        if r.get("status") == "duplicate":
            r["reason"] = "already there"    # not "already in your library": that is the owner's business
    return {"results": out}


@router.post("/share/{token}/upload/finish")
def shared_upload_finish(token: str):
    from ..engine.uploads import ensure_upload_root
    from ..pipeline import jobs

    conn, share = _require(token)
    if not share["allow_upload"]:
        raise HTTPException(403, "this link does not allow adding photos")
    _, root = ensure_upload_root(get_state().ctx, conn)
    jobs.spawn_index_job(get_state().ctx, {"kind": "index", "roots": [str(root)]})
    return {"ok": True}


@router.get("/share/{token}/thumb/{photo_id}")
def shared_thumb(token: str, photo_id: int, s: str = Query("m", pattern="^(sm|m|l)$")):
    from .images import thumb

    conn, share = _require(token)
    if not _in_share(conn, share, photo_id):
        raise HTTPException(404, "not in this album")
    return thumb(photo_id, s=s)


@router.get("/share/{token}/video/{photo_id}")
def shared_video(token: str, photo_id: int):
    from .routes_library import photo_video

    conn, share = _require(token)
    if not _in_share(conn, share, photo_id):
        raise HTTPException(404, "not in this album")
    return photo_video(photo_id)


@router.get("/share/{token}/download/{photo_id}")
def shared_download(token: str, photo_id: int):
    from .images import download

    conn, share = _require(token)
    if not share["allow_download"] or not _in_share(conn, share, photo_id):
        raise HTTPException(403, "downloads are not allowed for this link")
    return download(photo_id)
