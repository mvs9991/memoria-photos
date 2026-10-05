"""FastAPI application factory."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import accounts, auth, db
from ..context import AppContext
from . import dav, images, routes_accounts, routes_albums, routes_auth, routes_duplicates, routes_explore, routes_export, routes_trash, routes_upload, routes_events, routes_library, routes_people, routes_search, routes_service, routes_system
from .deps import ApiState, get_state, set_current_user, set_locked_open, set_state, start_request, start_response_notes

log = logging.getLogger(__name__)
WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


def _warm_models(ctx: AppContext) -> None:
    """Load the semantic model in the background so the first search is not a 15s wait."""
    try:
        _warm_models_now(ctx)
    finally:
        from .. import scheduler

        scheduler.WARM.set()                    # the first scheduled index may start now


def _warm_models_now(ctx: AppContext) -> None:
    # PyTorch first, here and not on the main thread: about 2 GB of libraries, which on a cold hard drive took
    # minutes, and the site did not answer at all until they were in. Anything else that needs it meanwhile waits
    # on vectors._torch's lock, so it is still only ever imported by one thread at a time.
    from ..vectors import _torch

    _torch()
    try:
        t0 = time.time()
        ctx.semantic_model()
        log.info("Semantic model warm after %.1fs", time.time() - t0)
    except Exception as exc:
        log.warning("Could not warm the semantic model: %s", exc)
    # ...and the photo embeddings every search and "similar photos" ranks against: loading ~29k of them
    # made the first "similar photos" after a restart take 2.6 s instead of ~50 ms.
    try:
        state = get_state()
        conn = ctx.connect()
        try:
            model_id = db.active_model_id(conn, "semantic")
            if model_id:
                index = state.index_cache.get(conn, model_id, int(db.get_meta(conn, "gen:embeddings", 0) or 0))
                # The first text query and the first nearest-neighbour search on the GPU each paid a one-off
                # start-up (1.3 s and 0.9 s on the real library); pay them here instead of in someone's search.
                t0 = time.time()
                ctx.semantic_model().encode_texts(["a photo"])
                if len(index.ids):
                    index.search(index.mat[0], k=2, device=ctx.device)
                log.info("Search warm after %.1fs", time.time() - t0)
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Could not preload photo embeddings: %s", exc)
    # People's merge suggestions compare every person's faces with their neighbours' (5-7 s on the real
    # library, on the first visit to People after a restart). Kept until faces or people change.
    try:
        from ..engine.people import merge_suggestions

        conn = ctx.connect()
        try:
            t0 = time.time()
            merge_suggestions(ctx, conn)
            log.info("Merge suggestions warm after %.1fs", time.time() - t0)
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Could not precompute merge suggestions: %s", exc)


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


_last_request = [time.monotonic()]
IDLE_FOR = 30.0


def _idle() -> bool:
    return time.monotonic() - _last_request[0] > IDLE_FOR


def _small_thumb_backfill(ctx: AppContext, stop: threading.Event) -> None:
    """Make the phone grid's small thumbnails for photos indexed before the indexer made them. Only while
    nobody has asked the server for anything for 30 s and no index job runs: it reads from the same slow
    disk as everyone else."""
    from ..engine import thumbpack, thumbs
    from ..pipeline import jobs

    while not stop.wait(60):
        if not _idle():
            continue
        try:
            conn = ctx.connect()
            try:
                if jobs.active_job(conn) is not None:
                    continue
                made = thumbs.backfill(conn, ctx.paths.thumbs, may_run=lambda: _idle() and not stop.is_set())
                if made:
                    log.info("Made %d small thumbnails while idle", made)
                elif not thumbs.missing(conn, ctx.paths.thumbs, 1):
                    # Every small thumbnail exists: keep the pack (grid order, one file) up to date, then rest.
                    if thumbpack.needs_build(conn, ctx.paths.thumbs):
                        thumbpack.build(conn, ctx.paths.thumbs, may_run=lambda: _idle() and not stop.is_set())
                    else:
                        stop.wait(3600)         # all done; look again in an hour (new imports make their own)
            finally:
                conn.close()
        except Exception:
            log.exception("Small thumbnail backfill failed")


def create_app(ctx: AppContext) -> FastAPI:
    set_state(ApiState(ctx))
    threading.Thread(target=_warm_models, args=(ctx,), daemon=True, name="warm-models").start()
    threading.Thread(target=_trash_sweeper, args=(ctx, threading.Event()), daemon=True, name="trash-sweeper").start()
    threading.Thread(target=_small_thumb_backfill, args=(ctx, threading.Event()), daemon=True,
                     name="small-thumbs").start()
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
        _last_request[0] = time.monotonic()
        start_request(request.url.path, "gzip" in request.headers.get("accept-encoding", "").lower())
        response = _hardened(await access(request, call_next))
        if request.url.path.startswith("/assets/") and response.status_code == 200:
            # Build files are named by their content, so a browser never needs to ask about them again.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    # Text is compressed for any client that says it can take it: the photo list was 950 KB of JSON
    # (147 KB compressed) and the app's script 420 KB, which is most of the wait on a phone over Wi-Fi.
    # Photos, videos, downloads and partial (range) responses are already compact or must stay
    # byte-exact, so they pass through untouched. Level 5: almost all of level 9's saving, far less CPU.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5, exclude_content_types=(
        "image/*", "video/*", "audio/*", "font/woff", "font/woff2", "text/event-stream",
        "application/octet-stream", "application/zip", "application/x-zip-compressed",
        "application/gzip", "application/x-gzip"))

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
