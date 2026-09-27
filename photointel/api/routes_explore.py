"""Collections (media types, clean-up queues), folders, insights, slideshow/frame, GPX, HTML export."""
from __future__ import annotations

import random
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query, UploadFile
from pydantic import BaseModel

from ..engine import albums as albums_mod
from ..engine import insights as insights_mod
from .deps import get_state
from .routes_library import COLLECTIONS, photo_filter_sql

router = APIRouter()


@router.get("/collections")
def collections():
    conn = get_state().conn()
    hidden = conn.execute("SELECT COUNT(*), MAX(id) FROM photos WHERE status = 'ok' AND hidden = 1 "
                          "AND live_component = 0").fetchone()
    out = [{"key": "hidden", "title": "Hidden", "group": "cleanup", "count": hidden[0], "cover_photo_id": None}]
    arch = conn.execute("SELECT COUNT(*), MAX(id) FROM photos WHERE status = 'ok' AND hidden = 0 AND archived = 1 "
                        "AND live_component = 0").fetchone()
    out.append({"key": "archive", "title": "Archive", "group": "media", "count": arch[0], "cover_photo_id": arch[1]})
    for key, spec in COLLECTIONS.items():
        where, args = photo_filter_sql(conn, collection=key)
        row = conn.execute(
            f"SELECT COUNT(*) n, (SELECT p2.id FROM photos p2 WHERE p2.id IN (SELECT p.id FROM photos p WHERE {where}) "
            f"ORDER BY p2.rating DESC, COALESCE(p2.quality_score, 0) DESC LIMIT 1) cover FROM photos p WHERE {where}",
            (*args, *args)).fetchone()
        out.append({"key": key, "title": spec["title"], "group": spec["group"], "count": row["n"],
                    "cover_photo_id": row["cover"]})
    recent = conn.execute("SELECT id FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 "
                          "ORDER BY first_seen_at DESC, id DESC LIMIT 1").fetchone()
    dups = conn.execute("SELECT COUNT(*) FROM dup_groups WHERE kind != 'similar' AND review_status = 'pending'").fetchone()[0]
    return {"collections": out, "recently_added_cover": recent[0] if recent else None, "duplicate_groups": dups}


@router.get("/folders/browse")
def browse_folders(root_id: int | None = None, path: str = ""):
    """Roots, or the immediate subfolders of `path` in a root, with counts and covers."""
    conn = get_state().conn()
    vis = "status = 'ok' AND hidden = 0 AND live_component = 0"
    if root_id is None:
        roots = []
        for r in conn.execute("SELECT id, path FROM roots ORDER BY id"):
            n, cover = conn.execute(
                f"SELECT COUNT(*), (SELECT id FROM photos WHERE root_id = ? AND {vis} "
                f"ORDER BY COALESCE(quality_score, 0) DESC LIMIT 1) FROM photos WHERE root_id = ? AND {vis}",
                (r["id"], r["id"])).fetchone()
            roots.append({"root_id": r["id"], "name": r["path"], "path": "", "count": n, "cover_photo_id": cover})
        return {"root_id": None, "path": "", "folders": roots, "direct_count": 0}
    prefix = path.strip("/")
    rows = conn.execute(
        f"SELECT folder, COUNT(*) n, MAX(COALESCE(quality_score, 0)) q FROM photos WHERE root_id = ? AND {vis} "
        f"AND (? = '' OR folder = ? OR folder LIKE ?) GROUP BY folder",
        (root_id, prefix, prefix, prefix + "/%")).fetchall()
    children: dict[str, int] = defaultdict(int)
    direct = 0
    for r in rows:
        folder = r["folder"]
        if folder == prefix:
            direct += r["n"]
            continue
        rest = folder[len(prefix) + 1:] if prefix else folder
        children[rest.split("/", 1)[0]] += r["n"]
    folders = []
    for name, n in sorted(children.items(), key=lambda kv: kv[0].lower()):
        full = f"{prefix}/{name}" if prefix else name
        cover = conn.execute(
            f"SELECT id FROM photos WHERE root_id = ? AND {vis} AND (folder = ? OR folder LIKE ?) "
            f"ORDER BY rating DESC, COALESCE(quality_score, 0) DESC LIMIT 1", (root_id, full, full + "/%")).fetchone()
        folders.append({"root_id": root_id, "name": name, "path": full, "count": n,
                        "cover_photo_id": cover[0] if cover else None})
    return {"root_id": root_id, "path": prefix, "folders": folders, "direct_count": direct}


@router.get("/insights")
def insights(year: int | None = None):
    return insights_mod.compute(get_state().conn(), year)


@router.get("/random")
def random_photos(count: int = Query(1, ge=1, le=500), album: int | None = None, person: int | None = None,
                  min_rating: int = Query(0, ge=0, le=5), collection: str | None = None, images_only: bool = True):
    """Random photos for slideshows and photo frames (also usable by Home Assistant et al.)."""
    conn = get_state().conn()
    if album is not None:
        pool = albums_mod.album_photo_ids(conn, album)
    else:
        where, args = photo_filter_sql(conn, person=[person] if person else None, min_rating=min_rating,
                                       collection=collection, include_screenshots=False, collapse_stacks=True,
                                       archived="exclude")
        if images_only:
            where += " AND p.media_type = 'image'"
        pool = [int(r[0]) for r in conn.execute(f"SELECT p.id FROM photos p WHERE {where}", args)]
    return {"ids": random.sample(pool, min(count, len(pool)))}


@router.get("/random/image")
def random_image(album: int | None = None, person: int | None = None, min_rating: int = 0,
                 collection: str | None = None):
    """One random photo as an image: point a frame or dashboard at this URL."""
    from .images import thumb

    ids = random_photos(1, album, person, min_rating, collection, True)["ids"]
    if not ids:
        raise HTTPException(404, "no photos match")
    resp = thumb(ids[0], s="l")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/gpx")
async def upload_gpx(file: UploadFile):
    """Add a GPS track (saved under the data directory, never beside the photos)."""
    import tempfile
    from pathlib import Path

    from ..engine.gpx import GpxError, import_file

    state = get_state()
    data = await file.read()
    if len(data) > 50 << 20:
        raise HTTPException(413, "track file too large")
    name = Path(file.filename or "track.gpx").name
    if not name.lower().endswith(".gpx"):
        raise HTTPException(400, "expected a .gpx file")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / name
        src.write_bytes(data)
        try:
            tid = import_file(state.ctx, state.conn(), src)
        except GpxError as exc:
            raise HTTPException(400, str(exc))
    return {"id": tid}


@router.get("/gpx/tracks")
def gpx_tracks(start_ts: float | None = None, end_ts: float | None = None):
    from ..engine.gpx import track_polylines

    return {"tracks": track_polylines(get_state().conn(), start_ts, end_ts)}


class HtmlExportBody(BaseModel):
    folder: str


@router.post("/albums/{album_id}/export-html")
def export_album_html(album_id: int, body: HtmlExportBody):
    from ..engine.html_export import ExportError, export_album

    state = get_state()
    conn = state.conn()
    from .deps import current_user_id

    a = albums_mod.can_see(conn, album_id, current_user_id())
    if a is None:
        raise HTTPException(404, "album not found")
    ids = (state.search.search(conn, a["query"], limit=5000).photo_ids if a["kind"] == "smart"
           else albums_mod.album_photo_ids(conn, album_id))
    try:
        return export_album(conn, ids, a["name"], body.folder, a["description"])
    except ExportError as exc:
        raise HTTPException(400, str(exc))
