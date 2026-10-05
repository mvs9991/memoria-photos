"""Uploading from a phone (or any browser) and backing up to another drive."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from ..engine import backup as backup_mod
from ..engine import uploads as uploads_mod
from ..engine.xmp import ExportError
from ..pipeline import jobs
from .deps import get_state

router = APIRouter()


@router.post("/upload")
def upload(files: list[UploadFile]):
    """One or more files. Each is kept, skipped as a duplicate, or rejected — never overwriting."""
    state = get_state()
    conn = state.conn()
    from .deps import current_role

    owner = current_role() == "owner"
    out = []
    for f in files:
        saved = uploads_mod.save_upload(state.ctx, conn, f.file, f.filename or "upload", who=_who(),
                                        sees_hidden=owner).__dict__
        if not owner:                     # the server's folders and the library's photo ids are the owner's
            saved.pop("path", None)
            saved.pop("photo_id", None)
        out.append(saved)
    return {"results": out, "folder": str(uploads_mod.upload_root(state.ctx)) if owner else ""}


def _who() -> str:
    from .deps import current_user

    user = current_user()
    return user or "Phone"


@router.post("/upload/finish")
def finish_upload():
    """Index what was just uploaded (only the upload folder is scanned)."""
    state = get_state()
    conn = state.conn()
    _, root = uploads_mod.ensure_upload_root(state.ctx, conn)
    return {"job_id": jobs.spawn_index_job(state.ctx, {"kind": "index", "roots": [str(root)]})}


class BackupBody(BaseModel):
    folder: str | None = None


@router.post("/backup")
def start_backup(body: BackupBody):
    from .. import scheduler

    state = get_state()
    target = (body.folder or state.ctx.settings.backup_folder or "").strip()
    if not target:
        raise HTTPException(400, "choose a backup folder")
    conn = state.conn()
    try:
        backup_mod.validate_target(state.ctx, conn, target)
    except ExportError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        # A folder that exists but cannot be written to: a system directory, a
        # read-only drive, a network share that has gone away. The caller's
        # problem to fix, not a server fault, so it is a 400 and not a 500.
        raise HTTPException(400, f"cannot write to that folder: {exc.strerror or exc}")
    if conn.execute("SELECT 1 FROM jobs WHERE kind = 'backup' AND status IN ('running', 'queued')").fetchone():
        raise HTTPException(409, "a backup is already running")
    return {"job_id": scheduler.start_backup(state.ctx, conn, target)}


@router.get("/backup")
def backup_status():
    state = get_state()
    return {"last": backup_mod.last_backup(state.conn()), "folder": state.ctx.settings.backup_folder,
            "every_days": state.ctx.settings.backup_every_days}


# ---------------------------------------------------------------- editing as copies

class EditBody(BaseModel):
    rotate: int = 0
    flip: bool = False
    crop: list[float] | None = None
    brightness: int = 0
    contrast: int = 0
    saturation: int = 0
    warmth: int = 0
    filter: str = "none"
    auto: bool = False


def _edit_spec(body: EditBody):
    from ..engine.editor import EditError, EditSpec

    try:
        return EditSpec.from_dict(body.model_dump())
    except (EditError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/photos/{photo_id}/edit/preview")
def edit_preview(photo_id: int, body: EditBody):
    from fastapi.responses import Response

    from ..engine.editor import EditError, preview
    from .deps import guard_locked

    conn = get_state().conn()
    row = conn.execute("SELECT locked, status FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "photo not found")
    guard_locked(row)
    try:
        return Response(preview(conn, photo_id, _edit_spec(body)), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except EditError as exc:
        raise HTTPException(400, str(exc))


@router.post("/photos/{photo_id}/edit")
def edit_save(photo_id: int, body: EditBody):
    """Save the edit as a new photo; the original is not touched."""
    from ..engine.editor import EditError, save_edit

    state = get_state()
    conn = state.conn()
    try:
        out = save_edit(state.ctx, conn, photo_id, _edit_spec(body))
    except EditError as exc:
        raise HTTPException(400, str(exc))
    _, root = uploads_mod.ensure_upload_root(state.ctx, conn)
    out["job_id"] = jobs.spawn_index_job(state.ctx, {"kind": "index", "roots": [str(root)]})
    from .deps import shown_path

    if "path" in out:
        out["path"] = shown_path(out["path"])     # the page shows only the file name
    return out


class TrimBody(BaseModel):
    start: float
    end: float


@router.post("/photos/{photo_id}/trim")
def trim(photo_id: int, body: TrimBody):
    """Save the part of a video between two times as a new video; the original is not touched."""
    from ..engine.editor import EditError, trim_video

    state = get_state()
    conn = state.conn()
    try:
        out = trim_video(state.ctx, conn, photo_id, body.start, body.end)
    except EditError as exc:
        raise HTTPException(400, str(exc))
    _, root = uploads_mod.ensure_upload_root(state.ctx, conn)
    out["job_id"] = jobs.spawn_index_job(state.ctx, {"kind": "index", "roots": [str(root)]})
    from .deps import shown_path

    out["path"] = shown_path(out.get("path"))
    return out


# ---------------------------------------------------------------- creations

class CreateBody(BaseModel):
    photo_ids: list[int]
    seconds_each: float = 3.0
    fps: int = 6


@router.post("/create/{kind}")
def create(kind: str, body: CreateBody):
    """A collage or an animation now; a movie as a job (it takes a while to encode)."""
    import threading

    from ..engine import creations

    state = get_state()
    conn = state.conn()
    if kind not in ("collage", "animation", "movie"):
        raise HTTPException(404, "unknown creation")
    try:
        if kind == "collage":
            out = creations.collage(state.ctx, conn, body.photo_ids)
        elif kind == "animation":
            out = creations.animation(state.ctx, conn, body.photo_ids, fps=body.fps)
        else:
            if not 2 <= len(body.photo_ids) <= 150:
                raise creations.CreateError("choose 2 to 150 photos for a movie")
            job_id = jobs.create_job(conn, "create", {"kind": "movie", "photos": len(body.photo_ids)})
            threading.Thread(target=_movie_job, args=(state.ctx, job_id, body.photo_ids, body.seconds_each),
                             daemon=True, name=f"movie-{job_id}").start()
            return {"job_id": job_id}
    except creations.CreateError as exc:
        raise HTTPException(400, str(exc))
    _, root = uploads_mod.ensure_upload_root(state.ctx, conn)
    out["job_id"] = jobs.spawn_index_job(state.ctx, {"kind": "index", "roots": [str(root)]})
    return out


def _movie_job(ctx, job_id: int, photo_ids: list[int], seconds_each: float) -> None:
    from ..engine import creations

    reporter = jobs.JobReporter(ctx, job_id)
    reporter.start()
    conn = ctx.connect()
    try:
        stop = lambda: bool(conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()[0])  # noqa: E731
        out = creations.movie(ctx, conn, photo_ids, seconds_each=seconds_each, should_stop=stop,
                              progress=lambda d, t: reporter.progress("movie", d, t, f"Made {d} of {t} photos"))
        reporter.finish("done", f"Made {out['path'].split(chr(92))[-1].split('/')[-1]}")
        _, root = uploads_mod.ensure_upload_root(ctx, conn)
        jobs.spawn_index_job(ctx, {"kind": "index", "roots": [str(root)]})
    except Exception as exc:
        reporter.finish("failed", "Could not make the movie", str(exc))
    finally:
        conn.close()
