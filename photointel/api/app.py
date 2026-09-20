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

from ..context import AppContext
from . import images, routes_duplicates, routes_events, routes_library, routes_people, routes_search, routes_system
from .deps import ApiState, set_state

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


def create_app(ctx: AppContext) -> FastAPI:
    set_state(ApiState(ctx))
    threading.Thread(target=_warm_models, args=(ctx,), daemon=True, name="warm-models").start()
    app = FastAPI(title="Memoria", version=__import__("photointel").__version__, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    # In a normal run the UI is served from this same origin, so no cross-origin
    # access is needed at all. Granting it unconditionally would let anything else
    # running on the dev port read the whole library, so it is opt-in.
    if os.environ.get("PHOTOINTEL_DEV") == "1":
        app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                           allow_methods=["*"], allow_headers=["*"])
        log.warning("PHOTOINTEL_DEV=1: allowing cross-origin requests from the Vite dev server")

    for router in (routes_library.router, routes_people.router, routes_events.router, routes_search.router,
                   routes_duplicates.router, routes_system.router, images.router):
        app.include_router(router, prefix="/api")

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
