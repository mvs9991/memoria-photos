"""A WebDAV drop box at /dav/ for phone backup apps (FolderSync, PhotoSync, and others
that can sync a camera folder to a WebDAV server): automatic backup without a Memoria app.

What a backup app needs, and nothing more:
* PUT stores a photo through the same path as the Upload page — filed by the date it was
  taken, skipped when its bytes are already in the library, never overwriting anything.
* PROPFIND lists back what that app uploaded, at the paths it used, so it can tell what is
  already backed up and does not send it again.
* MKCOL creates the (virtual) folders apps expect; MOVE renames, which apps use to finish an
  upload sent under a temporary name.
* GET / HEAD read back a file that was uploaded here.
* DELETE is refused: a phone deleting a photo must never delete the backup. Nothing here
  removes or changes a file in the library.

Sign-in is HTTP Basic with a Memoria account (or, without accounts, any name and the library
password); guests may only read. New uploads are indexed about a minute after the last one.
"""
from __future__ import annotations

import base64
import os
import tempfile
import time
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .. import accounts, auth, db, ratelimit
from ..config import SUPPORTED_EXTENSIONS
from ..engine import uploads as uploads_mod
from .deps import get_state

router = APIRouter()
PREFIX = "/dav/"
METHODS = ["OPTIONS", "PROPFIND", "MKCOL", "PUT", "GET", "HEAD", "MOVE", "DELETE", "COPY", "LOCK", "UNLOCK", "PROPPATCH"]


def _clean(path: str) -> str | None:
    parts = [p for p in unquote(path).replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _seen(conn, who: str) -> None:
    """When this backup app last connected (health.py warns when a phone goes quiet). At most
    one write per ten minutes per name: a sync makes hundreds of requests."""
    key = f"dav_seen:{who}"
    last = db.get_meta(conn, key)
    now = time.time()
    if not last or now - float(last) > 600:
        db.set_meta(conn, key, now)
        conn.commit()


def _who(request: Request) -> tuple[str | None, str | None, Response | None]:
    """-> (name, role, refusal). Basic auth against accounts, or the single library password."""
    state = get_state()
    ctx, conn = state.ctx, state.conn()
    protected = accounts.enabled(conn) or bool(ctx.settings.access_password_hash)
    header = request.headers.get("authorization", "")
    name = password = None
    if header.lower().startswith("basic "):
        try:
            name, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            name = None
    challenge = Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Memoria"'})
    if not protected:
        host = request.client.host if request.client else ""
        if not auth.is_loopback(host):
            return None, None, Response("set a password first", status_code=403)
        return (name or "Phone"), "owner", None
    if name is None:
        return None, None, challenge
    keys = (f"ip:{request.client.host if request.client else '?'}", f"user:{name.strip().lower()}")
    wait = ratelimit.LOGINS.wait_for(*keys)
    if wait > 0:
        return None, None, Response(ratelimit.refuse_message(wait), status_code=429)
    if accounts.enabled(conn):
        user = accounts.check_login(conn, name, password or "")
        if user is None:
            ratelimit.LOGINS.fail(*keys)
            return None, None, challenge
        ratelimit.LOGINS.succeed(*keys)
        return user["username"], user["role"], None
    if not auth.verify_password(password or "", ctx.settings.access_password_hash):
        ratelimit.LOGINS.fail(*keys)
        return None, None, challenge
    ratelimit.LOGINS.succeed(*keys)
    return (name.strip() or "Phone"), "owner", None


def _discard(tmp: Path) -> None:
    """Remove one of this drop box's own temp uploads from <upload folder>/.incoming. The only
    removal in this module, and it only ever receives a path that mkstemp made there."""
    tmp.unlink(missing_ok=True)


def _entry(conn, who: str, path: str):
    return conn.execute("SELECT * FROM dav_files WHERE who = ? AND path = ?", (who, path)).fetchone()


def _is_dir(conn, who: str, path: str) -> bool:
    if path == "":
        return True
    like = path.replace("%", r"\%").replace("_", r"\_") + "/%"
    return bool(conn.execute("SELECT 1 FROM dav_dirs WHERE who = ? AND path = ?", (who, path)).fetchone()
                or conn.execute("SELECT 1 FROM dav_files WHERE who = ? AND path LIKE ? ESCAPE '\\' LIMIT 1",
                                (who, like)).fetchone()
                or conn.execute("SELECT 1 FROM dav_dirs WHERE who = ? AND path LIKE ? ESCAPE '\\' LIMIT 1",
                                (who, like)).fetchone())


def _href(path: str, collection: bool) -> str:
    return PREFIX + quote(path) + ("/" if collection and path else "")


def _response_xml(path: str, collection: bool, size: int = 0, mtime: float | None = None) -> str:
    name = path.rsplit("/", 1)[-1] if path else "dav"
    props = [f"<D:displayname>{escape(name)}</D:displayname>",
             "<D:resourcetype><D:collection/></D:resourcetype>" if collection else "<D:resourcetype/>"]
    if not collection:
        props += [f"<D:getcontentlength>{size}</D:getcontentlength>",
                  "<D:getcontenttype>application/octet-stream</D:getcontenttype>",
                  f'<D:getetag>"{int(mtime or 0)}-{size}"</D:getetag>']
    props.append(f"<D:getlastmodified>{formatdate(mtime or time.time(), usegmt=True)}</D:getlastmodified>")
    return (f"<D:response><D:href>{escape(_href(path, collection))}</D:href><D:propstat><D:prop>{''.join(props)}"
            f"</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _propfind(conn, who: str, path: str, depth: str) -> Response:
    entry = _entry(conn, who, path)
    if entry is None and not _is_dir(conn, who, path):
        return Response(status_code=404)
    parts = []
    if entry is not None:
        parts.append(_response_xml(path, False, entry["size"], entry["mtime"]))
    else:
        parts.append(_response_xml(path, True))
        if depth != "0":
            base = f"{path}/" if path else ""
            like = base.replace("%", r"\%").replace("_", r"\_") + "%"
            seen_dirs: set[str] = set()
            for r in conn.execute("SELECT path, size, mtime FROM dav_files WHERE who = ? AND path LIKE ? ESCAPE '\\'",
                                  (who, like)):
                rest = r["path"][len(base):]
                if "/" in rest:
                    seen_dirs.add(base + rest.split("/", 1)[0])
                else:
                    parts.append(_response_xml(r["path"], False, r["size"], r["mtime"]))
            for (d,) in conn.execute("SELECT path FROM dav_dirs WHERE who = ? AND path LIKE ? ESCAPE '\\'", (who, like)):
                rest = d[len(base):]
                if rest:
                    seen_dirs.add(base + rest.split("/", 1)[0])
            parts += [_response_xml(d, True) for d in sorted(seen_dirs)]
    body = '<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">' + "".join(parts) + "</D:multistatus>"
    return Response(body, status_code=207, media_type='application/xml; charset="utf-8"')


def _mark_pending(conn) -> None:
    if not db.get_meta(conn, "uploads_pending_since"):
        db.set_meta(conn, "uploads_pending_since", time.time())
        conn.commit()


def _finalize(conn, who: str, path: str, tmp: Path, size: int, role: str = "owner") -> tuple[int, str]:
    """Store a received file under its final name; record where it went for that path."""
    ctx = get_state().ctx
    name = path.rsplit("/", 1)[-1]
    with open(tmp, "rb") as fh:
        saved = uploads_mod.save_upload(ctx, conn, fh, name, who=who, sees_hidden=role == "owner")
    if saved.status == "rejected":
        return 415, saved.reason or "not a photo or video"
    stored = saved.path
    if stored is None and saved.photo_id is not None:
        row = conn.execute("SELECT p.rel_path, r.path AS root FROM photos p JOIN roots r ON r.id = p.root_id "
                           "WHERE p.id = ?", (saved.photo_id,)).fetchone()
        stored = str(Path(row["root"]) / row["rel_path"]) if row else None
    existed = _entry(conn, who, path) is not None
    conn.execute("INSERT OR REPLACE INTO dav_files(who, path, stored_path, size, mtime, uploaded_at) VALUES (?,?,?,?,?,?)",
                 (who, path, stored or "", size, time.time(), time.time()))
    conn.commit()
    if saved.status == "added":
        _mark_pending(conn)
    return (204 if existed else 201), saved.status


# Kept out of the OpenAPI schema. WebDAV is a protocol endpoint, not part of the REST
# API, and FastAPI builds an operation id from one arbitrary member of a route's method
# set — so all twelve methods here collided on a single id, filling /api/docs with 24
# bogus "dav" entries and breaking any generated client.
@router.api_route("/dav", methods=METHODS, include_in_schema=False)
@router.api_route("/dav/{path:path}", methods=METHODS, include_in_schema=False)
async def dav(request: Request, path: str = ""):
    method = request.method.upper()
    if method == "OPTIONS":
        return Response(headers={"DAV": "1", "Allow": ", ".join(METHODS), "MS-Author-Via": "DAV"})
    who, role, refusal = await run_in_threadpool(_who, request)
    if refusal is not None:
        return refusal
    clean = _clean(path)
    if clean is None:
        return Response(status_code=400)
    conn = get_state().conn()
    _seen(conn, who)
    writes = method in ("PUT", "MKCOL", "MOVE", "COPY", "PROPPATCH")
    if writes and role == "guest":
        return Response("your account can only look", status_code=403)

    if method == "PROPFIND":
        return await run_in_threadpool(_propfind, conn, who, clean, request.headers.get("depth", "1"))
    if method in ("GET", "HEAD"):
        e = _entry(conn, who, clean)
        if e is None:
            if _is_dir(conn, who, clean):
                return Response("Memoria backup folder", media_type="text/plain")
            return Response(status_code=404)
        target = Path(e["stored_path"]) if e["stored_path"] else None
        if target is None or not target.is_file():
            pend = conn.execute("SELECT tmp_path FROM dav_pending WHERE who = ? AND path = ?", (who, clean)).fetchone()
            target = Path(pend[0]) if pend else None
        if target is None or not target.is_file():
            return Response(status_code=404)
        return FileResponse(target, media_type="application/octet-stream")
    if method == "MKCOL":
        if _entry(conn, who, clean) is not None or _is_dir(conn, who, clean):
            return Response(status_code=405)
        conn.execute("INSERT OR IGNORE INTO dav_dirs(who, path) VALUES (?, ?)", (who, clean))
        conn.commit()
        return Response(status_code=201)
    if method == "DELETE":
        return Response("Memoria never deletes from a backup: remove photos in the app's Trash instead.",
                        status_code=403)
    if method in ("COPY", "LOCK", "UNLOCK", "PROPPATCH"):
        return Response(status_code=501)
    if method == "PUT":
        if not clean or _is_dir(conn, who, clean) and _entry(conn, who, clean) is None:
            return Response(status_code=409)
        ctx = get_state().ctx
        _, root = uploads_mod.ensure_upload_root(ctx, conn)
        incoming = root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        ext = Path(clean).suffix.lower()
        fd, tmp_name = tempfile.mkstemp(dir=incoming, suffix=ext if ext in SUPPORTED_EXTENSIONS else ".part")
        size = 0
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > uploads_mod.MAX_BYTES:
                        break
                    out.write(chunk)
        except BaseException:
            # The phone dropped the connection (or the disk filled) part-way: a half photo must
            # not be left in .incoming for ever.
            _discard(tmp)
            raise
        if size > uploads_mod.MAX_BYTES:
            _discard(tmp)
            return Response("file is too large", status_code=413)
        if ext not in SUPPORTED_EXTENSIONS:
            # Probably a temporary name the app will MOVE into place; keep it until then. A retry
            # of the same temporary name replaces the earlier bytes, so drop those.
            old = conn.execute("SELECT tmp_path FROM dav_pending WHERE who = ? AND path = ?", (who, clean)).fetchone()
            conn.execute("INSERT OR REPLACE INTO dav_pending(who, path, tmp_path, size, created_at) VALUES (?,?,?,?,?)",
                         (who, clean, str(tmp), size, time.time()))
            conn.commit()
            if old is not None and old[0] != str(tmp):
                _discard(Path(old[0]))
            return Response(status_code=201)
        try:
            code, _ = await run_in_threadpool(_finalize, conn, who, clean, tmp, size, role)
        finally:
            _discard(tmp)
        return Response(status_code=code)
    if method == "MOVE":
        dest_url = request.headers.get("destination", "")
        dest_path = urlparse(dest_url).path if "://" in dest_url else dest_url
        if not dest_path.startswith(PREFIX.rstrip("/")):
            return Response(status_code=502)
        dest = _clean(dest_path[len(PREFIX.rstrip("/")):])
        if not dest:
            return Response(status_code=400)
        pend = conn.execute("SELECT * FROM dav_pending WHERE who = ? AND path = ?", (who, clean)).fetchone()
        if pend is not None:
            tmp = Path(pend["tmp_path"])
            if not tmp.is_file():
                return Response(status_code=404)
            try:
                code, _ = await run_in_threadpool(_finalize, conn, who, dest, tmp, pend["size"], role)
            finally:
                _discard(tmp)
                conn.execute("DELETE FROM dav_pending WHERE who = ? AND path = ?", (who, clean))
                conn.commit()
            return Response(status_code=code)
        e = _entry(conn, who, clean)
        if e is None:
            return Response(status_code=404)
        existed = _entry(conn, who, dest) is not None
        conn.execute("DELETE FROM dav_files WHERE who = ? AND path = ?", (who, dest))
        conn.execute("UPDATE dav_files SET path = ? WHERE who = ? AND path = ?", (dest, who, clean))
        conn.commit()
        return Response(status_code=204 if existed else 201)
    return Response(status_code=405)

