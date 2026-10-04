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
from pydantic import BaseModel, Field

from .. import db
from ..config import SUPPORTED_EXTENSIONS
from ..pipeline import jobs as jobs_mod
from ..pipeline.scanner import ensure_root
from .deps import current_user_id, get_state

log = logging.getLogger(__name__)
router = APIRouter()


# What an app screen needs to know, for someone who is not the owner (no folders or paths).
MEMBER_SETTINGS = ("allow_online_map_tiles", "allow_delete", "trash_days", "auto_index_minutes")


@router.get("/settings")
def get_settings():
    from .deps import current_role

    state = get_state()
    s = state.ctx.settings
    conn = state.conn()
    if current_role() != "owner":
        pub = s.public_dict()
        return {"settings": {k: pub[k] for k in MEMBER_SETTINGS}, "roots": [], "data_dir": "",
                "supported_extensions": sorted(SUPPORTED_EXTENSIONS)}
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
    ocr_enabled: bool | None = None
    takeout_import: bool | None = None
    stacks_enabled: bool | None = None
    allow_delete: bool | None = None
    trash_days: int | None = Field(None, ge=1, le=365)
    auto_index_minutes: int | None = Field(None, ge=0, le=10080)
    upload_folder: str | None = None
    backup_folder: str | None = None
    backup_every_days: int | None = Field(None, ge=0, le=365)
    serve_host: str | None = Field(None, pattern=r"^(127\.0\.0\.1|0\.0\.0\.0)$")
    keep_awake: bool | None = None
    alert_webhook: str | None = Field(None, max_length=500, pattern=r"^(|https?://\S+)$")
    offsite_every_days: int | None = Field(None, ge=0, le=365)


@router.post("/settings")
def update_settings(body: SettingsBody):
    from .. import accounts
    from ..auth import is_loopback

    state = get_state()
    s = state.ctx.settings
    if body.serve_host and not is_loopback(body.serve_host) and not (
            s.access_password_hash or accounts.enabled(state.conn())):
        # The next start would refuse (and the keeper would rightly not retry): Memoria would be off.
        raise HTTPException(400, "set a password (or turn on accounts) before opening Memoria to the network")
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
    try:
        # As in /browse: an impossible path raises here rather than answering False.
        if not p.is_dir():
            raise HTTPException(400, f"Not a folder: {p}")
    except OSError as exc:
        raise HTTPException(400, f"Not a folder: {exc.strerror or exc}")
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
    try:
        # is_dir() does not merely answer False for an impossible path: a name past
        # the filesystem's limit raises ENAMETOOLONG, which reached the caller as a
        # 500. Linux raises where Windows does not, so only CI caught it.
        if not p.is_dir():
            raise HTTPException(400, "not a folder")
    except OSError:
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
    except OSError as exc:
        # A symlink loop, a folder that vanished mid-listing, a disconnected share.
        raise HTTPException(400, f"cannot list that folder: {exc.strerror or exc}")
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "entries": entries[:500]}


class JobBody(BaseModel):
    kind: str = "index"
    roots: list[str] | None = None
    retry_errors: bool = False
    post_only: bool = False
    skip_faces: bool = False
    skip_semantic: bool = False
    all: bool = False               # ocr: read every photo, not only likely-text ones
    stages: list[str] | None = None # post_only: run just these post stages


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


# a row of `photos` joined as p: not locked, and not private to anyone but the person asking
_NOT_SOMEONES_ELSE = "(p.id IS NULL OR (p.locked = 0 AND (p.private_to IS NULL OR p.private_to = ?)))"


@router.get("/errors")
def errors(limit: int = 200):
    """A file that failed to read keeps status 'error' but still belongs to whoever uploaded it
    privately, or to the Locked folder, so its name and path are not for anyone else's eyes."""
    conn = get_state().conn()
    me = current_user_id() or 0
    rows = conn.execute(
        """SELECT e.id, e.photo_id, e.path, e.stage, e.error, e.created_at, p.filename, p.status
           FROM processing_errors e LEFT JOIN photos p ON p.id = e.photo_id
           WHERE {unseen_free}
           ORDER BY e.id DESC LIMIT ?""".format(unseen_free=_NOT_SOMEONES_ELSE), (me, limit)).fetchall()
    return {"errors": [dict(r) for r in rows],
            "total": conn.execute("SELECT COUNT(*) FROM processing_errors e LEFT JOIN photos p ON p.id = e.photo_id "
                                  "WHERE " + _NOT_SOMEONES_ELSE, (me,)).fetchone()[0]}


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
    return {"entries": _without_unseen_photos(conn, [dict(r) for r in conn.execute(sql, args)])}


_ID_LISTS = ("photos", "photo_ids", "members", "ids")


def _without_unseen_photos(conn, entries: list[dict]) -> list[dict]:
    """The log is written when an action happens, so it names photos (ids, captions) that were
    visible then and are locked or private now. Entries about such a photo are left out, and its id is
    taken out of any list in another entry's details (an album, a tag, a stack)."""
    import json

    unseen = {int(r[0]) for r in conn.execute("SELECT id FROM photos WHERE status IN ('locked', 'private')")}
    if not unseen:
        return entries
    out = []
    for e in entries:
        if e["entity_type"] == "photo" and e["entity_id"] in unseen:
            continue
        try:
            details = json.loads(e["details"]) if e.get("details") else None
        except ValueError:
            details = None
        if isinstance(details, dict):
            for k in _ID_LISTS:
                if isinstance(details.get(k), list):
                    details[k] = [x for x in details[k] if x not in unseen]
            e = {**e, "details": json.dumps(details)}
        out.append(e)
    return out


_CACHE_BYTES_TTL = 60.0
_cache_bytes_memo: dict = {}


def _dir_bytes(root) -> int:
    """Size of a folder tree, tolerating files that vanish mid-walk (a cache clear, a thumbnail rewrite)."""
    total = 0
    for dirpath, _dirs, names in os.walk(root, onerror=lambda _e: None):
        for name in names:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _cache_bytes(folders) -> int:
    """Walking every thumbnail of a large library took 11 s on each /api/health call (and a file deleted
    during the walk made it a 500). The figure is only shown on Settings, so a minute-old one is fine."""
    key = tuple(str(f) for f in folders)
    hit = _cache_bytes_memo.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_BYTES_TTL:
        return hit[1]
    value = sum(_dir_bytes(f) for f in folders if os.path.isdir(f))
    _cache_bytes_memo[key] = (now, value)
    return value


@router.get("/health")
def health():
    state = get_state()
    conn = state.conn()
    paths = state.ctx.paths
    cache_bytes = _cache_bytes((paths.thumbs, paths.previews, paths.faces))
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
    if kind in ("thumbs", "all"):
        from ..engine import thumbs

        thumbs._known.clear()            # the idle backfill should make them again, not assume they exist
    return {"removed": removed}
