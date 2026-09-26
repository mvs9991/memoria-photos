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
    out = []
    for f in files:
        saved = uploads_mod.save_upload(state.ctx, conn, f.file, f.filename or "upload", who=_who())
        out.append(saved.__dict__)
    return {"results": out, "folder": str(uploads_mod.upload_root(state.ctx))}


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
    if conn.execute("SELECT 1 FROM jobs WHERE kind = 'backup' AND status IN ('running', 'queued')").fetchone():
        raise HTTPException(409, "a backup is already running")
    return {"job_id": scheduler.start_backup(state.ctx, conn, target)}


@router.get("/backup")
def backup_status():
    state = get_state()
    return {"last": backup_mod.last_backup(state.conn()), "folder": state.ctx.settings.backup_folder,
            "every_days": state.ctx.settings.backup_every_days}
