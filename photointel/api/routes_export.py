"""Export copies of originals: preview what a selection holds, copy it to a folder (a
tracked job), or stream it as a .zip — the way a phone or another computer gets files."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..engine import export as export_mod
from ..engine.xmp import ExportError, _check_destination
from ..pipeline.jobs import create_job
from .deps import get_state

router = APIRouter()


class SpecBody(BaseModel):
    photo_ids: list[int] | None = None
    album_id: int | None = None
    event_id: int | None = None
    person_ids: list[int] | None = None
    person_mode: str = "each"
    year: int | None = None
    month: int | None = None
    layout: str = "date"
    include_live: bool = True
    include_stack_frames: bool = False
    xmp: bool = False
    folder: str | None = None


def _spec(body: dict) -> export_mod.ExportSpec:
    state = get_state()
    conn = state.conn()
    if body.get("album_id") is not None:
        from ..engine.albums import can_see
        from .deps import current_user_id

        a = can_see(conn, body["album_id"], current_user_id())
        if a is None:
            raise HTTPException(404, "album not found")
        if a["kind"] == "smart":          # a smart album is a saved search: export what it shows now
            body = {**body, "album_id": None,
                    "photo_ids": state.search.search(conn, a["query"], limit=100_000).photo_ids}
    try:
        return export_mod.ExportSpec.from_dict(body)
    except (ExportError, TypeError) as exc:
        raise HTTPException(400, str(exc))


def default_export_dir(ctx) -> Path:
    """Where exports go unless told otherwise: the configured folder, else <data>/exports."""
    configured = (getattr(ctx.settings, "export_dir", "") or "").strip()
    return Path(configured).expanduser().resolve() if configured else ctx.paths.exports


@router.get("/export/location")
def export_location():
    """The folder the UI pre-fills, plus a dated sub-folder so each export stays separate
    and can be picked up whole — copied to a drive, or sent on."""
    ctx = get_state().ctx
    base = default_export_dir(ctx)
    suggested = base / time.strftime("%Y-%m-%d %H%M")
    return {"base": str(base), "suggested": str(suggested),
            "configured": bool((getattr(ctx.settings, "export_dir", "") or "").strip())}


@router.post("/export/preview")
def preview(body: SpecBody):
    spec = _spec(body.model_dump())
    p = export_mod.plan(get_state().conn(), spec)
    return {"items": len(p.items), "bytes": p.bytes, "groups": p.groups}


@router.post("/export")
def start_export(body: SpecBody):
    """Copy to a folder on this computer, as a job (progress and cancel like indexing)."""
    state = get_state()
    spec = _spec(body.model_dump())
    conn = state.conn()
    if not spec.folder:
        # No folder given: fall back to the export location rather than refusing, so
        # "Export" alone always has somewhere sensible to put the copies.
        spec.folder = str(default_export_dir(get_state().ctx) / time.strftime("%Y-%m-%d %H%M"))
    try:
        _check_destination(conn, Path(spec.folder))
    except ExportError as exc:
        raise HTTPException(400, str(exc))
    job_id = create_job(conn, "export", {k: v for k, v in body.model_dump().items() if k != "photo_ids"}
                        | {"photos": len(body.photo_ids or [])})
    threading.Thread(target=_run, args=(state.ctx, job_id, spec), daemon=True, name=f"export-{job_id}").start()
    return {"job_id": job_id}


def _run(ctx, job_id: int, spec) -> None:
    try:
        export_mod.run_job(ctx, job_id, spec)
    except Exception:
        pass      # recorded on the job by run_job


@router.post("/export/zip")
def export_zip(spec: str = Form(...)):
    """A form post (so the browser handles the download itself) carrying the spec as JSON."""
    try:
        body = SpecBody(**json.loads(spec)).model_dump()
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"bad export request: {exc}")
    s = _spec(body)
    try:
        stream = export_mod.iter_zip(get_state().conn(), s)
    except ExportError as exc:
        raise HTTPException(400, str(exc))
    name = f"memoria-{time.strftime('%Y%m%d-%H%M')}.zip"
    return StreamingResponse(stream, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
