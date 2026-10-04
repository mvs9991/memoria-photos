"""FastAPI application factory."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import accounts, auth
from ..context import AppContext
from . import dav, images, routes_accounts, routes_albums, routes_auth, routes_duplicates, routes_explore, routes_export, routes_trash, routes_upload, routes_events, routes_library, routes_people, routes_search, routes_service, routes_system
from .deps import ApiState, get_state, set_current_user, set_locked_open, set_state, start_request, start_response_notes

log = logging.getLogger(__name__)
WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


def _warm_models(ctx: AppContext) -> None:
    """Load the semantic model in the background so the first search is not a 15s wait."""
    try:
        t0 = time.time()
        ctx.semantic_model()
        log.info("Semantic model warm after %.1fs", time.time() - t0)
    except Exception as exc:
        log.warning("Could not warm the semantic model: %s", exc)


def _trash_sweeper(ctx: AppContext, stop: threading.Event) -> None:
    """Finish or undo a delete interrupted by a crash, then erase what has been in the trash
    longer than Settings.trash_days — at start-up and every six hours. This is the only
    automatic erase, and it only touches files the user deleted themselves."""
    from ..engine import trash

    first = True
    while first or not stop.wait(6 * 3600):
        first = False
        try:
            conn = ctx.connect()
            try:
                trash.reconcile(conn)
                trash.purge_expired(ctx, conn)
            finally:
                conn.close()
        except Exception:
            log.exception("Trash sweep failed")


def create_app(ctx: AppContext) -> FastAPI:
    set_state(ApiState(ctx))
    threading.Thread(target=_warm_models, args=(ctx,), daemon=True, name="warm-models").start()
    threading.Thread(target=_trash_sweeper, args=(ctx, threading.Event()), daemon=True, name="trash-sweeper").start()
    from .. import scheduler

    threading.Thread(target=scheduler.run, args=(ctx, threading.Event()), daemon=True, name="scheduler").start()
    app = FastAPI(title="Memoria", version=__import__("photointel").__version__, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    # In a normal run the UI is served from this same origin, so no cross-origin
    # access is needed at all. Granting it unconditionally would let anything else
    # running on the dev port read the whole library, so it is opt-in.
    if os.environ.get("PHOTOINTEL_DEV") == "1":
        app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                           allow_methods=["*"], allow_headers=["*"])
        log.warning("PHOTOINTEL_DEV=1: allowing cross-origin requests from the Vite dev server")

    for router in (routes_auth.router, routes_accounts.router, routes_explore.router, routes_export.router, routes_trash.router, routes_upload.router, routes_library.router, routes_albums.router, routes_people.router, routes_events.router, routes_search.router,
                   routes_duplicates.router, routes_system.router, routes_service.router, images.router):
        app.include_router(router, prefix="/api")
    app.include_router(dav.router)          # /dav/: the backup-app drop box (its own sign-in)

    def _hardened(response):
        """Headers every response carries: no guessing a type for what we serve (a photo or upload can
        never be run as a page), no framing by another site (clickjacking), and no leaking the address
        of a page, which can hold a share token, to sites it links to."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        start_request(request.url.path)
        return _hardened(await access(request, call_next))

    async def access(request: Request, call_next):
        """Who is asking and whether they may. With accounts, every API route needs a signed-in
        account and its role must allow the request; with just a password, a session; with
        neither (only reachable from this machine by default), everyone is the owner. Logging
        in and share links (which check their own token) are open."""
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        public = path.startswith(("/api/auth/", "/api/share/"))
        token = request.cookies.get(auth.SESSION_COOKIE)
        conn = get_state().conn()
        user = None
        if accounts.enabled(conn):
            user = accounts.from_session(ctx, conn, token)
            if user is None and not public:
                return JSONResponse({"detail": "login required"}, status_code=401)
        elif ctx.settings.access_password_hash and not public and not auth.valid_session(ctx.paths.data, token):
            return JSONResponse({"detail": "login required"}, status_code=401)
        role = user["role"] if user else "owner"
        if not public and not accounts.allowed(role, request.method, path):
            return JSONResponse({"detail": "your account cannot do this"}, status_code=403)
        set_current_user(user)
        set_locked_open(role == "owner" and auth.valid_locked_token(
            ctx.paths.data, request.cookies.get(auth.LOCKED_COOKIE)))
        notes = start_response_notes()
        response = await call_next(request)
        if notes.get("no_store"):             # a locked photo: see deps.guard_locked
            response.headers["Cache-Control"] = "no-store"
        if request.url.scheme == "https":
            # Over HTTPS (directly or behind a local proxy), cookies must never travel in the clear.
            response.raw_headers[:] = [
                (k, v + b"; Secure") if k.lower() == b"set-cookie" and b"secure" not in v.lower() else (k, v)
                for k, v in response.raw_headers]
        return response

    @app.exception_handler(OverflowError)
    async def too_big(request: Request, exc: OverflowError):
        """A number past what SQLite (or C) can hold is a bad request, not a crash.

        FastAPI validates `int` with Python's arbitrary-precision ints, so a 21-digit
        id sails through the signature and only fails when SQLite is asked to bind it.
        That turned roughly thirty-five read endpoints into a 500 and a stack trace for
        anyone who mistyped a URL. Handled centrally so endpoints added later inherit it.
        """
        return JSONResponse({"detail": "a number in this request is out of range"},
                            status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover - safety net
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=500)

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            # An unknown /api/ path is a mistake, not a client-side route. Falling
            # through to index.html answered 200 with HTML, so a caller expecting
            # JSON got "Unexpected token <" instead of a plain 404.
            if full_path == "api" or full_path.startswith("api/"):
                return JSONResponse({"detail": "no such endpoint"}, status_code=404)
            candidate = WEB_DIST / full_path
            try:
                is_file = bool(full_path) and candidate.is_file()
            except OSError:
                # A path longer than the filesystem allows raises rather than
                # answering False (ENAMETOOLONG on Linux, where it 500'd; Windows
                # happened not to). It is simply not a file we serve.
                is_file = False
            if is_file:
                # The service worker and the page must be re-checked on every visit, or a
                # phone keeps running an old build (hashed /assets/ files can be kept forever).
                fresh = full_path in ("sw.js", "index.html", "manifest.json")
                return FileResponse(candidate, headers={"Cache-Control": "no-cache"} if fresh else None)
            return FileResponse(WEB_DIST / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/")
        def placeholder():
            return {"message": "Memoria API is running. Build the web UI with: cd web && npm install && npm run build"}

    return app
