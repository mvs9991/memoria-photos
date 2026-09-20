"""Settings, jobs, models, diagnostics."""
from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..config import SUPPORTED_EXTENSIONS
from ..pipeline import jobs as jobs_mod
from ..pipeline.scanner import ensure_root
from .deps import get_state

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/settings")
def get_settings():
    state = get_state()
    s = state.ctx.settings
    conn = state.conn()
    return {
        "settings": s.public_dict(),
        "roots": [dict(r) for r in conn.execute("SELECT id, path, added_at, last_scan_at FROM roots")],
        "data_dir": str(state.ctx.paths.data),
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
    }


class SettingsBody(BaseModel):
    device: str | None = None
    workers: int | None = None
    face_det_size: int | None = None
    face_min_size_px: int | None = None
    cluster_min_faces: int | None = None
    event_min_photos: int | None = None
    allow_online_map_tiles: bool | None = None
    llm_enabled: bool | None = None
    llm_send_images: bool | None = None
    llm_model: str | None = None
    anthropic_api_key: str | None = None
    me_person_id: int | None = None
    thumb_size: int | None = None


@router.post("/settings")
def update_settings(body: SettingsBody):
    state = get_state()
    s = state.ctx.settings
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None or field == "me_person_id":
            setattr(s, field, value)
    s.save(state.ctx.paths.data)
    return {"ok": True, "settings": s.public_dict()}


class RootBody(BaseModel):
    path: str


@router.post("/roots")
def add_root(body: RootBody):
    state = get_state()
    p = Path(body.path).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"Not a folder: {p}")
    try:
        if state.ctx.paths.data.resolve() in p.resolve().parents or p.resolve() == state.ctx.paths.data.resolve():
            raise HTTPException(400, "Choose a folder outside the Memoria data directory")
    except OSError:
        pass
    conn = state.conn()
    rid = ensure_root(conn, p)
    return {"id": rid, "path": str(p.resolve())}


@router.delete("/roots/{root_id}")
def remove_root(root_id: int, delete_photos: bool = Query(True)):
    conn = get_state().conn()
    if delete_photos:
        conn.execute("DELETE FROM photos WHERE root_id=?", (root_id,))
    conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
    db.audit(conn, "root_removed", "root", root_id, {})
    conn.commit()
    return {"ok": True}


@router.get("/browse")
def browse(path: str | None = None):
    """Minimal folder browser so the user can pick a library root in the UI."""
    if not path:
        if os.name == "nt":
            drives = []
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                d = f"{letter}:\\"
                if os.path.exists(d):
                    drives.append({"name": d, "path": d, "is_dir": True})
            home = str(Path.home())
            return {"path": None, "parent": None,
                    "entries": [{"name": "Home", "path": home, "is_dir": True}] + drives}
        path = str(Path.home())
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(400, "not a folder")
    entries = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    entries.append({"name": e.name, "path": e.path, "is_dir": True})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "permission denied")
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "entries": entries[:500]}


class JobBody(BaseModel):
    kind: str = "index"
    roots: list[str] | None = None
    retry_errors: bool = False
    post_only: bool = False
    skip_faces: bool = False
    skip_semantic: bool = False


@router.post("/jobs")
def start_job(body: JobBody):
    state = get_state()
    job_id = jobs_mod.spawn_index_job(state.ctx, body.model_dump())
    return {"job_id": job_id}


@router.get("/jobs")
def list_jobs(limit: int = 20):
    conn = get_state().conn()
    jobs_mod.reap_stale_jobs(conn)
    rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"jobs": [dict(r) for r in rows], "active": jobs_mod.active_job(conn)}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    conn = get_state().conn()
    jobs_mod.cancel_job(conn, job_id)
    return {"ok": True}


@router.get("/models")
def models():
    state = get_state()
    conn = state.conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM models ORDER BY kind, id")]
    active = {kind: db.active_model_id(conn, kind) for kind in ("face", "semantic", "caption")}
    for r in rows:
        r["active"] = active.get(r["kind"]) == r["id"]
        if r["kind"] == "face":
            r["usage"] = conn.execute("SELECT COUNT(*) FROM faces WHERE model_id=?", (r["id"],)).fetchone()[0]
        elif r["kind"] == "semantic":
            r["usage"] = conn.execute("SELECT COUNT(*) FROM photo_embeddings WHERE model_id=?",
                                      (r["id"],)).fetchone()[0]
        else:
            r["usage"] = 0
    return {"models": rows, "active": active, "device": state.ctx.device}


@router.get("/errors")
def errors(limit: int = 200):
    conn = get_state().conn()
    rows = conn.execute(
        """SELECT e.id, e.photo_id, e.path, e.stage, e.error, e.created_at, p.filename, p.status
           FROM processing_errors e LEFT JOIN photos p ON p.id = e.photo_id
           ORDER BY e.id DESC LIMIT ?""", (limit,)).fetchall()
    return {"errors": [dict(r) for r in rows],
            "total": conn.execute("SELECT COUNT(*) FROM processing_errors").fetchone()[0]}


@router.get("/audit")
def audit(limit: int = 100, entity_type: str | None = None):
    conn = get_state().conn()
    sql = "SELECT * FROM audit_log"
    args: list = []
    if entity_type:
        sql += " WHERE entity_type = ?"
        args.append(entity_type)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return {"entries": [dict(r) for r in conn.execute(sql, args)]}


@router.get("/health")
def health():
    state = get_state()
    conn = state.conn()
    paths = state.ctx.paths
    cache_bytes = 0
    for d in (paths.thumbs, paths.previews, paths.faces):
        if d.exists():
            cache_bytes += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    try:
        free = shutil.disk_usage(paths.data).free
    except OSError:
        free = 0
    return {
        "ok": True,
        "version": __import__("photointel").__version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "device": state.ctx.device,
        "db_bytes": paths.db.stat().st_size if paths.db.exists() else 0,
        "cache_bytes": cache_bytes,
        "free_bytes": free,
        "geo_data": (paths.geo / "cities500.txt").exists() or (paths.geo / "cities_cache.npz").exists(),
        "face_models": (state.ctx.face_model_dir / "det_10g.onnx").exists(),
        "llm_configured": bool(state.ctx.settings.anthropic_api_key),
        "active_job": jobs_mod.active_job(conn),
    }


@router.post("/cache/clear")
def clear_cache(kind: str = Query("previews", pattern="^(previews|faces|thumbs|all)$")):
    """Thumbnails regenerate on demand; clearing is safe (originals are untouched)."""
    state = get_state()
    paths = state.ctx.paths
    targets = {"previews": [paths.previews], "faces": [paths.faces], "thumbs": [paths.thumbs],
               "all": [paths.previews, paths.faces, paths.thumbs]}[kind]
    removed = 0
    for t in targets:
        if t.exists():
            for f in t.rglob("*"):
                if f.is_file():
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
    return {"removed": removed}
