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
from . import images, routes_accounts, routes_albums, routes_auth, routes_duplicates, routes_explore, routes_export, routes_trash, routes_upload, routes_events, routes_library, routes_people, routes_search, routes_system
from .deps import ApiState, get_state, set_current_user, set_locked_open, set_state

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
                   routes_duplicates.router, routes_system.router, images.router):
        app.include_router(router, prefix="/api")

    @app.middleware("http")
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
        response = await call_next(request)
        if request.url.scheme == "https":
            # Over HTTPS (directly or behind a local proxy), cookies must never travel in the clear.
            response.raw_headers[:] = [
                (k, v + b"; Secure") if k.lower() == b"set-cookie" and b"secure" not in v.lower() else (k, v)
                for k, v in response.raw_headers]
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover - safety net
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=500)

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            candidate = WEB_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")
    else:
        @app.get("/")
        def placeholder():
            return {"message": "Memoria API is running. Build the web UI with: cd web && npm install && npm run build"}

    return app
