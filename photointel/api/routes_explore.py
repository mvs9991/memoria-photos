"""Collections (media types, clean-up queues), folders, insights, slideshow/frame, GPX, HTML export."""
from __future__ import annotations

import random
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query, UploadFile
from pydantic import BaseModel

from .. import db
from ..engine import albums as albums_mod
from ..engine import insights as insights_mod
from .deps import get_state, shown_path
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
    # Every collection shares the same base filter and differs only by its own clause,
    # so all the counts come from one pass instead of one scan each. At 250k photos
    # that was 14 scans and about five seconds.
    # The covers come from the same pass: each collection's best photo (rating, then quality, then lowest id).
    # A cover query per collection after the counts doubled the time (about a second more on the real library).
    base, base_args = photo_filter_sql(conn, collection=None)
    keys = list(COLLECTIONS)
    flags = ", ".join(f"CASE WHEN {COLLECTIONS[key]['where']} THEN 1 ELSE 0 END" for key in keys)
    counts = dict.fromkeys(keys, 0)
    best: dict[str, tuple] = {}
    for row in conn.execute(f"SELECT p.id, p.rating, COALESCE(p.quality_score, 0), {flags} FROM photos p "
                            f"WHERE {base}", base_args):
        rank = (row[1] or 0, row[2], -row[0])
        for n_key, key in enumerate(keys):
            if row[3 + n_key]:
                counts[key] += 1
                if key not in best or rank > best[key]:
                    best[key] = rank
    for key, spec in COLLECTIONS.items():
        out.append({"key": key, "title": spec["title"], "group": spec["group"], "count": counts[key],
                    "cover_photo_id": -best[key][2] if key in best else None})
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
        # Every root's count and cover (best quality, the lowest id on a tie) from one pass, not two scans
        # per root.
        count: dict[int, int] = defaultdict(int)
        best: dict[int, tuple] = {}
        for rid, pid, quality in conn.execute(
                f"SELECT root_id, id, COALESCE(quality_score, 0) FROM photos WHERE {vis} ORDER BY id"):
            count[rid] += 1
            if rid not in best or quality > best[rid][0]:
                best[rid] = (quality, pid)
        roots = [{"root_id": r["id"], "name": shown_path(r["path"]), "path": "", "count": count.get(r["id"], 0),
                  "cover_photo_id": best[r["id"]][1] if r["id"] in best else None}
                 for r in conn.execute("SELECT id, path FROM roots ORDER BY id")]
        return {"root_id": None, "path": "", "folders": roots, "direct_count": 0}
    prefix = path.strip("/")
    # One pass for the counts and every subfolder's cover (best rating, then quality; on a tie the first in
    # id order, which is what the per-subfolder ORDER BY ... LIMIT 1 query this replaces returned).
    # That query ran once per subfolder, with a LIKE no index serves: 0.6 s for a 30-folder root.
    rows = conn.execute(
        f"SELECT folder, id, rating, COALESCE(quality_score, 0) FROM photos WHERE root_id = ? AND {vis} "
        f"AND (? = '' OR folder = ? OR folder LIKE ? ESCAPE '\\') ORDER BY folder",
        (root_id, prefix, prefix, db.like_prefix(prefix))).fetchall()
    rows.sort(key=lambda r: r[1])      # then id order (ORDER BY id made SQLite walk the whole table)
    children: dict[str, int] = defaultdict(int)
    best: dict[str, tuple] = {}
    direct = 0
    for folder, pid, rating, quality in rows:
        if folder == prefix:
            direct += 1
            continue
        rest = folder[len(prefix) + 1:] if prefix else folder
        name = rest.split("/", 1)[0]
        children[name] += 1
        if name not in best or (rating, quality) > best[name][0]:
            best[name] = ((rating, quality), pid)
    folders = []
    for name, n in sorted(children.items(), key=lambda kv: kv[0].lower()):
        full = f"{prefix}/{name}" if prefix else name
        folders.append({"root_id": root_id, "name": name, "path": full, "count": n,
                        "cover_photo_id": best[name][1] if name in best else None})
    return {"root_id": root_id, "path": prefix, "folders": folders, "direct_count": direct}


@router.get("/insights")
# Bounded because the year reaches datetime(), which raises for anything outside
# 1-9999 — a 500 for a mistyped URL. 1826 is the year of the first photograph.
def insights(year: int | None = Query(None, ge=1826, le=2200)):
    return insights_mod.compute(get_state().conn(), year)


@router.get("/random")
def random_photos(count: int = Query(1, ge=1, le=500), album: int | None = None, person: int | None = None,
                  min_rating: int = Query(0, ge=0, le=5), collection: str | None = None, images_only: bool = True):
    """Random photos for slideshows and photo frames (also usable by Home Assistant et al.)."""
    conn = get_state().conn()
    if album is not None:
        from .deps import current_user_id

        # Only an album this account may see (it took any id: one family member could pull random photos out
        # of another's private album), and a smart album shows what its search finds now.
        a = albums_mod.can_see(conn, album, current_user_id())
        if a is None:
            raise HTTPException(404, "album not found")
        pool = (get_state().search.search(conn, a["query"], limit=5000).photo_ids if a["kind"] == "smart"
                else albums_mod.album_photo_ids(conn, album))
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
    except OSError as exc:
        # A folder that exists but cannot be written to: a system directory, a
        # read-only drive, a network share that has gone away. The caller's
        # problem to fix, not a server fault, so it is a 400 and not a 500.
        raise HTTPException(400, f"cannot write to that folder: {exc.strerror or exc}")
