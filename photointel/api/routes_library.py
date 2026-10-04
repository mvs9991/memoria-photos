"""Photos, timeline, memories and library statistics."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import db
from ..engine import albums as albums_mod
from ..engine import corrections as corrections_mod
from ..engine import stacks as stacks_mod
from ..engine.events import date_range_label, event_title
from ..engine.people import person_label
from ..engine.places import place_label
from ..metadata import ts_to_naive
from ..rotation import rotate_box
from ..engine import favorites
from .deps import current_user_id, get_state, guard_locked

log = logging.getLogger(__name__)
router = APIRouter()


def photo_filter_sql(conn, person: list[int] | None = None, place: int | None = None, event: int | None = None,
                     year: int | None = None, month: int | None = None, tag: str | None = None,
                     source: str | None = None, favorite: bool = False, camera: str | None = None,
                     folder: str | None = None, has_faces: bool | None = None,
                     include_screenshots: bool = True, media: str | None = None, min_rating: int = 0,
                     collapse_stacks: bool = False, collection: str | None = None, root_id: int | None = None,
                     folder_exact: bool = False, archived: str = "include") -> tuple[str, list]:
    where = ["p.status = 'ok'", "p.hidden = 0", "p.live_component = 0"]
    args: list = []
    if archived == "exclude":            # the timeline: archived photos stay out of it
        where.append("p.archived = 0")
    elif archived == "only":
        where.append("p.archived = 1")
    if collection == "hidden":           # the one view that shows what "hide" took away
        where[1] = "p.hidden = 1"
    elif collection == "archive":
        where.append("p.archived = 1")
    elif collection:
        spec = COLLECTIONS.get(collection)
        if spec is None:
            raise HTTPException(400, f"unknown collection {collection!r}")
        where.append(spec["where"])
        if collection == "screenshots":
            include_screenshots = True
    if root_id is not None:
        where.append("p.root_id = ?")
        args.append(root_id)
    for pid in person or []:
        where.append("EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id AND f.person_id = ?)")
        args.append(pid)
    if place:
        from ..search.engine import SearchEngine

        ids = SearchEngine._expand_places(conn, {place})
        where.append(f"p.place_id IN ({','.join('?' * len(ids))})")
        args.extend(ids)
    if event:
        where.append("(p.event_id = ? OR EXISTS (SELECT 1 FROM trip_photos tp WHERE tp.photo_id = p.id "
                     "AND tp.trip_id = ?))")
        args.extend([event, event])
    if year:
        from ..metadata import naive_to_ts

        # Validated here rather than on each route: seven endpoints share this
        # filter, and datetime() raises for a year outside 1-9999 or a month outside
        # 1-12 — which reached the caller as a 500 for a merely mistyped URL.
        if not 1826 <= year <= 2200:
            raise HTTPException(422, "year must be between 1826 and 2200")
        if month is not None and not 1 <= month <= 12:
            raise HTTPException(422, "month must be between 1 and 12")
        start = datetime(year, month or 1, 1)
        if month:
            end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
        else:
            end = datetime(year + 1, 1, 1)
        where.append("p.taken_ts >= ? AND p.taken_ts < ?")
        args.extend([naive_to_ts(start), naive_to_ts(end)])
    if tag:
        where.append("EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
                     "WHERE pt.photo_id = p.id AND t.name = ? AND pt.score >= 2.0)")
        args.append(tag)
    if source:
        where.append("p.source_kind = ?")
        args.append(source)
    elif not include_screenshots:
        where.append("COALESCE(p.source_kind,'') != 'screenshot'")
    if camera:
        where.append("p.camera_model = ?")
        args.append(camera)
    if folder is not None and folder_exact:
        where.append("p.folder = ?")
        args.append(folder)
    elif folder:
        where.append("(p.folder = ? OR p.folder LIKE ?)")
        args.extend([folder, folder + "/%"])
    if favorite:
        where.append(f"{favorites.expr('p', current_user_id())} = 1")
    if has_faces is True:
        where.append("p.face_count > 0")
    if min_rating:
        where.append("p.rating >= ?")
        args.append(int(min_rating))
    if collapse_stacks:
        where.append("p.stack_hidden = 0")
    if media == "video":
        where.append("p.media_type = 'video'")
    elif media == "image":
        where.append("p.media_type = 'image'")
    elif media == "live":
        where.append("(p.live_video_id IS NOT NULL OR COALESCE(p.motion_offset, 0) > 0)")
    return " AND ".join(where), args


# Named sets of photos for the Collections page (media types) and clean-up review queues.
# The clean-up thresholds are heuristics, not measured detectors — the UI says "review".
_TEXTY = "('document', 'receipt', 'id card', 'whiteboard')"
COLLECTIONS: dict[str, dict] = {
    "videos": {"group": "media", "title": "Videos", "where": "p.media_type = 'video'"},
    "live": {"group": "media", "title": "Live & motion photos",
             "where": "(p.live_video_id IS NOT NULL OR COALESCE(p.motion_offset, 0) > 0)"},
    "panoramas": {"group": "media", "title": "Panoramas",
                  "where": "p.media_type = 'image' AND p.width > 0 AND p.height > 0 "
                           "AND (p.width * 1.0 / p.height >= 2.0 OR p.height * 1.0 / p.width >= 2.5) "
                           "AND COALESCE(p.source_kind, '') != 'screenshot'"},
    "selfies": {"group": "media", "title": "Selfies",
                "where": "EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id AND (f.x2 - f.x1) > 0.22) "
                         "AND p.face_count <= 2"},
    "raw": {"group": "media", "title": "RAW",
            "where": "p.ext IN ('.cr2','.cr3','.nef','.arw','.dng','.orf','.rw2','.raf','.srw','.pef','.nrw')"},
    "stacks": {"group": "media", "title": "Stacks", "where": "p.stack_id = p.id"},
    # Pets: the tagger's dog and cat. Which dog is not recognised: that needs a pet-identity
    # model, and none that runs offline is part of Memoria.
    "pets": {"group": "media", "title": "Pets",
             "where": "EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE "
                      "pt.photo_id = p.id AND t.name IN ('dog', 'cat') AND pt.score >= 2.0)"},
    "screenshots": {"group": "cleanup", "title": "Screenshots", "where": "p.source_kind = 'screenshot'"},
    "documents": {"group": "cleanup", "title": "Documents & receipts",
                  "where": "EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE "
                           f"pt.photo_id = p.id AND t.name IN {_TEXTY} AND pt.score >= 2.0)"},
    "memes": {"group": "cleanup", "title": "Memes & forwards",
              "where": "EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE "
                       "pt.photo_id = p.id AND t.name = 'meme' AND pt.score >= 2.0)"},
    "blurry": {"group": "cleanup", "title": "Possibly blurry",
               "where": "p.media_type = 'image' AND p.blur IS NOT NULL AND p.blur < 35 "
                        "AND COALESCE(p.source_kind, '') != 'screenshot'"},
    "large": {"group": "cleanup", "title": "Large files", "where": "p.size >= 20971520"},
    "no_location": {"group": "cleanup", "title": "No location", "where": "p.gps_lat IS NULL AND p.place_id IS NULL"},
    "no_date": {"group": "cleanup", "title": "Unsure of the date", "where": "p.date_confidence = 'low'"},
}


# Bit flags in /photos/index: favourite, has faces, video, live/motion photo, stack cover.
FLAG_FAVORITE, FLAG_FACES, FLAG_VIDEO, FLAG_LIVE, FLAG_STACK = 1, 2, 4, 8, 16


def photo_flags(r) -> int:
    return ((FLAG_FAVORITE if r["favorite"] else 0) | (FLAG_FACES if (r["face_count"] or 0) > 0 else 0)
            | (FLAG_VIDEO if r["media_type"] == "video" else 0)
            | (FLAG_LIVE if r["live_video_id"] or (r["motion_offset"] or 0) > 0 else 0)
            | (FLAG_STACK if _get(r, "stack_size", 0) > 1 else 0))


def _get(r, key: str, default=None):
    return r[key] if key in r.keys() else default


@router.get("/photos/index")
def photos_index(person: list[int] = Query(default=[]), place: int | None = None, event: int | None = None,
                 year: int | None = None, month: int | None = None, tag: str | None = None,
                 source: str | None = None, favorite: bool = False, camera: str | None = None,
                 folder: str | None = None, has_faces: bool | None = None, include_screenshots: bool = True,
                 media: str | None = Query(None, pattern="^(image|video|live)$"),
                 min_rating: int = Query(0, ge=0, le=5), collapse_stacks: bool = False,
                 order: str = Query("date_desc", pattern="^(date_desc|date_asc|quality|added|size)$"),
                 collection: str | None = None, root_id: int | None = None, folder_exact: bool = False,
                 archived: str = Query("include", pattern="^(include|exclude|only)$"),
                 limit: int = Query(200000, le=500000)):
    """Columnar photo list for the virtualised grid: ids, aspect ratios, timestamps.

    Compact on purpose — a 100k-photo library is ~1 MB of JSON (far less gzipped),
    which lets the client lay out and scrub the whole timeline without paging.
    """
    state = get_state()
    conn = state.conn()
    where, args = photo_filter_sql(
        conn, person=person, place=place, event=event, year=year, month=month, tag=tag, source=source,
        favorite=favorite, camera=camera, folder=folder, has_faces=has_faces,
        include_screenshots=include_screenshots, media=media, min_rating=min_rating,
        collapse_stacks=collapse_stacks, collection=collection, root_id=root_id, folder_exact=folder_exact,
        archived=archived)
    order_sql = {"date_desc": "p.taken_ts DESC, p.id DESC", "date_asc": "p.taken_ts ASC, p.id ASC",
                 # The user's stars outrank any computed score.
                 "quality": "p.rating DESC, COALESCE(p.quality_score,0) DESC",
                 "added": "p.first_seen_at DESC, p.id DESC",
                 "size": "p.size DESC"}[order]
    rows = conn.execute(
        f"SELECT p.id, p.width, p.height, p.rotation, p.taken_ts, p.face_count, "
        f"{favorites.expr('p', current_user_id())} AS favorite, p.media_type, p.duration, "
        f"p.live_video_id, p.motion_offset, p.rating, p.stack_id, "
        f"(SELECT COUNT(*) FROM photos s WHERE s.stack_id = p.id) AS stack_size FROM photos p "
        f"WHERE {where} ORDER BY {order_sql} LIMIT ?", (*args, limit)).fetchall()
    return columnar(rows)


def columnar(rows) -> dict:
    """The compact grid payload shared by every photo list (library, albums, events)."""
    ids, ratios, ts, flags, dur, stars, stack, rots = [], [], [], [], [], [], [], []
    for r in rows:
        ids.append(r["id"])
        w, h = r["width"] or 4, r["height"] or 3
        rot = _get(r, "rotation", 0) or 0
        if rot in (90, 270):
            w, h = h, w
        rots.append(rot)
        ratios.append(round(max(0.2, min(6.0, w / max(h, 1))), 3))
        ts.append(int(r["taken_ts"] or 0))
        flags.append(photo_flags(r))
        dur.append(round(r["duration"], 1) if r["media_type"] == "video" and r["duration"] else 0)
        stars.append(_get(r, "rating", 0) or 0)
        stack.append(_get(r, "stack_size", 0) or 0)
    return {"ids": ids, "ratio": ratios, "ts": ts, "flags": flags, "dur": dur, "rating": stars,
            "stack": stack, "rot": rots, "total": len(ids)}


@router.get("/photos/{photo_id}")
def photo_detail(photo_id: int):
    state = get_state()
    conn = state.conn()
    row = conn.execute(
        """SELECT p.*, r.path AS root, pl.name AS place_name, pl.city, pl.admin1, pl.admin2, pl.country,
                  pl.lat AS place_lat, pl.lon AS place_lon, lm.name AS landmark_name,
                  e.id AS event_id2, e.auto_title, e.user_title, e.kind AS event_kind, e.parent_id
           FROM photos p JOIN roots r ON r.id = p.root_id
           LEFT JOIN places pl ON pl.id = p.place_id
           LEFT JOIN places lm ON lm.id = p.landmark_id
           LEFT JOIN events e ON e.id = p.event_id WHERE p.id = ?""", (photo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "photo not found")
    guard_locked(row)
    faces = []
    from ..engine.people import age_on

    for f in conn.execute(
            """SELECT f.*, pe.name, pe.display_no, pe.id AS pid, pe.birth_date FROM faces f
               LEFT JOIN persons pe ON pe.id = f.person_id WHERE f.photo_id = ? ORDER BY f.size_px DESC""",
            (photo_id,)):
        faces.append({
            "id": f["id"], "box": rotate_box([f["x1"], f["y1"], f["x2"], f["y2"]], row["rotation"]), "person_id": f["pid"],
            "label": person_label(f) if f["pid"] else None, "confidence": f["assign_confidence"],
            "assign_source": f["assign_source"], "quality": f["quality"], "det_score": f["det_score"],
            "age": age_on(f["birth_date"], row["taken_ts"]) if f["pid"] else None,
        })
    from ..engine.tags import confidence as _tag_conf

    tags = [{"name": t["name"], "category": t["category"], "score": round(t["score"], 3),
             "confidence": round(_tag_conf(t["score"]), 3), "by_user": t["source"] == "user"} for t in conn.execute(
        """SELECT t.name, t.category, pt.score, pt.source FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
           WHERE pt.photo_id = ? AND pt.score > 0
           ORDER BY (pt.source = 'user') DESC, pt.score DESC LIMIT 16""", (photo_id,))]
    dups = []
    for d in conn.execute(
            """SELECT g.id, g.kind, g.member_count, g.keep_photo_id, m.relation FROM dup_members m
               JOIN dup_groups g ON g.id = m.group_id WHERE m.photo_id = ?""", (photo_id,)):
        dups.append({"group_id": d["id"], "kind": d["kind"], "count": d["member_count"],
                     "relation": d["relation"], "keep_photo_id": d["keep_photo_id"]})
    trip = None
    if row["parent_id"]:
        t = conn.execute("SELECT id, auto_title, user_title FROM events WHERE id=?", (row["parent_id"],)).fetchone()
        if t:
            trip = {"id": t["id"], "title": event_title(t)}
    place = None
    if row["place_id"]:
        place = {"id": row["place_id"], "name": row["place_name"], "city": row["city"], "admin1": row["admin1"],
                 "country": row["country"], "lat": row["place_lat"], "lon": row["place_lon"],
                 "label": place_label(conn.execute("SELECT * FROM places WHERE id=?", (row["place_id"],)).fetchone(),
                                      include_country=True),
                 "confidence": row["location_confidence"], "source": row["location_source"]}
    return {
        "id": row["id"], "filename": row["filename"], "folder": row["folder"], "path": row["rel_path"],
        "root": row["root"], "ext": row["ext"], "size": row["size"],
        "width": row["height"] if row["rotation"] in (90, 270) else row["width"],
        "height": row["width"] if row["rotation"] in (90, 270) else row["height"],
        "rotation": row["rotation"] or 0,
        "format": row["format"], "orientation": row["orientation"], "status": row["status"], "error": row["error"],
        "taken_ts": row["taken_ts"], "taken_local": row["taken_local"], "date_source": row["date_source"],
        "date_confidence": row["date_confidence"], "tz_offset_min": row["tz_offset_min"],
        "camera": {"make": row["camera_make"], "model": row["camera_model"], "lens": row["lens"],
                   "focal_length": row["focal_length"], "aperture": row["aperture"],
                   "exposure_time": row["exposure_time"], "iso": row["iso"], "software": row["software"]},
        "gps": {"lat": row["gps_lat"], "lon": row["gps_lon"], "alt": row["gps_alt"]} if row["gps_lat"] else None,
        "place": place, "landmark": row["landmark_name"], "source_kind": row["source_kind"],
        "quality": {"score": row["quality_score"], "blur": row["blur"], "brightness": row["brightness"],
                    "contrast": row["contrast"], "clipped": row["clipped"], "aesthetic": row["aesthetic"]},
        "caption": row["caption"], "faces": faces, "tags": tags, "duplicates": dups, "trip": trip,
        "favorite": favorites.is_favorite(conn, photo_id, current_user_id()), "hidden": bool(row["hidden"]),
        "event": {"id": row["event_id2"], "title": event_title(row) if row["event_id2"] else None,
                  "kind": row["event_kind"]} if row["event_id2"] else None,
        "sha256": row["sha256"],
        "media_type": row["media_type"], "duration": row["duration"], "video_codec": row["video_codec"],
        "live": bool(row["live_video_id"] or (row["motion_offset"] or 0) > 0),
        "description": row["description"], "ocr_text": row["ocr_text"] or None,
        "albums": albums_mod.albums_for_photo(conn, photo_id, current_user_id()),
        "rating": row["rating"] or 0,
        "stack": ({"id": row["stack_id"], "members": stacks_mod.members(conn, row["stack_id"])}
                  if row["stack_id"] else None),
        "corrected": {"date": row["date_source"] == "user", "location": row["location_source"] == "user"},
    }


# ----------------------------------------------------------------------------- video & motion

@router.get("/photos/{photo_id}/video")
def photo_video(photo_id: int):
    """Play a video. Browser-playable files are streamed as-is (with Range support);
    anything else is transcoded once to an H.264 preview in the cache."""
    from ..video import VideoError, playable_in_browser, transcode_to_mp4
    from .images import abs_path

    state = get_state()
    row = state.conn().execute(
        "SELECT p.*, r.path AS root FROM photos p JOIN roots r ON r.id = p.root_id WHERE p.id = ?",
        (photo_id,)).fetchone()
    if row is None or row["media_type"] != "video":
        raise HTTPException(404, "not a video")
    guard_locked(row)
    path = abs_path(row)
    if not path.exists():
        raise HTTPException(410, "original file is missing")
    if playable_in_browser(row["video_codec"], row["ext"]):
        return FileResponse(path, media_type="video/webm" if row["ext"] == ".webm" else "video/mp4",
                            headers={"Cache-Control": "private, max-age=3600"})
    cached = state.ctx.paths.data / "cache" / "videos" / (row["sha256"] or str(photo_id))[:2] / \
        f"{row['sha256'] or photo_id}.mp4"
    if not cached.exists():
        try:
            transcode_to_mp4(path, cached)
        except VideoError as exc:
            raise HTTPException(415, str(exc))
    return FileResponse(cached, media_type="video/mp4", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/photos/{photo_id}/motion")
def photo_motion(photo_id: int):
    """The moving part of a Live photo (its paired video) or a motion photo (embedded MP4)."""
    from ..video import motion_bytes
    from .images import abs_path

    conn = get_state().conn()
    row = conn.execute(
        "SELECT p.*, r.path AS root FROM photos p JOIN roots r ON r.id = p.root_id WHERE p.id = ?",
        (photo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "photo not found")
    guard_locked(row)
    if row["live_video_id"]:
        return photo_video(int(row["live_video_id"]))
    if (row["motion_offset"] or 0) > 0:
        path = abs_path(row)
        if not path.exists():
            raise HTTPException(410, "original file is missing")
        return Response(motion_bytes(path, row["motion_offset"]), media_type="video/mp4",
                        headers={"Cache-Control": "private, max-age=86400"})
    raise HTTPException(404, "this photo has no motion")


# ----------------------------------------------------------------------------- user tags

class TagBody(BaseModel):
    photo_ids: list[int]
    name: str


@router.post("/photos/tags")
def add_tag(body: TagBody):
    if not body.photo_ids or not body.name.strip():
        raise HTTPException(400, "photos and a tag name are required")
    return albums_mod.add_user_tag(get_state().conn(), body.photo_ids, body.name)


@router.post("/photos/tags/remove")
def remove_tag(body: TagBody):
    return albums_mod.remove_user_tag(get_state().conn(), body.photo_ids, body.name)


@router.get("/tags")
def list_tags():
    return {"tags": albums_mod.list_tags(get_state().conn())}


class HideBody(BaseModel):
    photo_ids: list[int]
    hidden: bool = True


@router.post("/photos/hide")
def hide_photos(body: HideBody):
    """Hide from (or bring back to) the library views. Files are never touched."""
    if not body.photo_ids:
        return {"changed": 0}
    conn = get_state().conn()
    marks = ",".join("?" * len(body.photo_ids))
    n = conn.execute(f"UPDATE photos SET hidden = ? WHERE id IN ({marks})", (int(body.hidden), *body.photo_ids)).rowcount
    db.audit(conn, "photos_hidden" if body.hidden else "photos_unhidden", "photo", None, {"photos": body.photo_ids[:2000]})
    conn.commit()
    from ..engine import visibility

    visibility.refresh(conn, body.photo_ids)
    return {"changed": n}


class RotateBody(BaseModel):
    photo_ids: list[int]
    degrees: int = 90          # clockwise; -90 turns left


@router.post("/photos/rotate")
def rotate_photos(body: RotateBody):
    """Turn photos as Memoria shows them. The files are not changed."""
    if body.degrees % 90:
        raise HTTPException(400, "rotate by a multiple of 90 degrees")
    if not body.photo_ids:
        return {"rotated": 0}
    conn = get_state().conn()
    marks = ",".join("?" * len(body.photo_ids))
    n = conn.execute(f"UPDATE photos SET rotation = ((rotation + ?) % 360 + 360) % 360 WHERE id IN ({marks})",
                     (body.degrees, *body.photo_ids)).rowcount
    db.audit(conn, "photos_rotated", "photo", None, {"photos": body.photo_ids[:2000], "degrees": body.degrees})
    conn.commit()
    return {"rotated": n}


class RateBody(BaseModel):
    photo_ids: list[int]
    rating: int


@router.post("/photos/rate")
def rate(body: RateBody):
    if not 0 <= body.rating <= 5:
        raise HTTPException(400, "rating is 0 (none) to 5")
    conn = get_state().conn()
    marks = ",".join("?" * len(body.photo_ids))
    conn.execute(f"UPDATE photos SET rating = ? WHERE id IN ({marks})", (body.rating, *body.photo_ids))
    db.audit(conn, "photos_rated", "photo", None, {"photos": body.photo_ids[:2000], "rating": body.rating})
    conn.commit()
    return {"rated": len(body.photo_ids), "rating": body.rating}


class DateFixBody(BaseModel):
    photo_ids: list[int]
    taken_local: str | None = None      # absolute, e.g. "2019-12-25 18:00"
    shift_seconds: float | None = None  # or move each photo by this much (a wrong camera clock)
    rebuild_events: bool = True


class PlaceFixBody(BaseModel):
    photo_ids: list[int]
    lat: float | None = None
    lon: float | None = None
    place_id: int | None = None         # or copy a known place's coordinates
    rebuild_events: bool = True


def _after_correction(rebuild: bool, stages: list[str]) -> None:
    if rebuild:
        from ..pipeline import jobs as jobs_mod

        jobs_mod.spawn_index_job(get_state().ctx, {"kind": "index", "post_only": True, "stages": stages})


@router.post("/photos/correct-date")
def correct_date(body: DateFixBody):
    if not body.photo_ids:
        raise HTTPException(400, "no photos given")
    try:
        out = corrections_mod.set_date(get_state().conn(), body.photo_ids, body.taken_local, body.shift_seconds)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _after_correction(body.rebuild_events, ["events", "stacks", "search-index"])
    return out


@router.post("/photos/correct-location")
def correct_location(body: PlaceFixBody):
    state = get_state()
    conn = state.conn()
    lat, lon = body.lat, body.lon
    if body.place_id is not None:
        pl = conn.execute("SELECT lat, lon FROM places WHERE id = ?", (body.place_id,)).fetchone()
        if pl is None or pl["lat"] is None:
            raise HTTPException(404, "place not found")
        lat, lon = pl["lat"], pl["lon"]
    if lat is None or lon is None or not body.photo_ids:
        raise HTTPException(400, "photos and a location are required")
    try:
        out = corrections_mod.set_location(conn, body.photo_ids, lat, lon)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    from ..engine.places import geocode_photos

    out["geocode"] = geocode_photos(state.ctx, conn)   # name the place now, not after the next index
    _after_correction(body.rebuild_events, ["events", "search-index"])
    return out


class IdsBody(BaseModel):
    photo_ids: list[int]


@router.post("/photos/corrections/clear")
def clear_corrections(body: IdsBody):
    return corrections_mod.clear(get_state().conn(), body.photo_ids)


@router.get("/stacks/{stack_id}")
def stack_members(stack_id: int):
    ids = stacks_mod.members(get_state().conn(), stack_id)
    if not ids:
        raise HTTPException(404, "no such stack")
    return {"id": stack_id, "members": ids}


class CoverBody(BaseModel):
    photo_id: int


@router.post("/stacks/{stack_id}/cover")
def stack_cover(stack_id: int, body: CoverBody):
    try:
        stacks_mod.set_cover(get_state().conn(), body.photo_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "stack_id": body.photo_id}


@router.post("/stacks/{stack_id}/unstack")
def stack_split(stack_id: int):
    stacks_mod.unstack(get_state().conn(), stack_id)
    return {"ok": True}


class ExportBody(BaseModel):
    folder: str
    include_auto_tags: bool = False
    everything: bool = False


@router.post("/export/xmp")
def export_xmp(body: ExportBody):
    from ..engine.xmp import ExportError
    from ..engine.xmp import export_xmp as run_export

    try:
        return run_export(get_state().conn(), body.folder, body.include_auto_tags, body.everything)
    except ExportError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        # A folder that exists but cannot be written to: a system directory, a
        # read-only drive, a network share that has gone away. The caller's
        # problem to fix, not a server fault, so it is a 400 and not a 500.
        raise HTTPException(400, f"cannot write to that folder: {exc.strerror or exc}")


class DescriptionBody(BaseModel):
    description: str | None = None


@router.post("/photos/{photo_id}/description")
def set_description(photo_id: int, body: DescriptionBody):
    _visible_photo(get_state().conn(), photo_id)
    conn = get_state().conn()
    text = (body.description or "").strip() or None
    conn.execute("UPDATE photos SET description = ? WHERE id = ?", (text, photo_id))
    db.audit(conn, "photo_described", "photo", photo_id, {"description": text})
    conn.commit()
    return {"ok": True, "description": text}


@router.get("/photos/{photo_id}/similar")
def similar_photos(photo_id: int, limit: int = Query(24, le=100)):
    _visible_photo(get_state().conn(), photo_id)
    state = get_state()
    conn = state.conn()
    model_id = db.active_model_id(conn, "semantic")
    if not model_id:
        return {"photos": []}
    index = state.index_cache.get(conn, model_id, state.generation("embeddings"))
    row_of = index.id_to_row()
    r = row_of.get(photo_id)
    if r is None:
        return {"photos": []}
    ids, sims = index.search(index.mat[r], k=limit + 1, device=state.ctx.device)
    out = [{"id": int(i), "score": round(float(s), 4)} for i, s in zip(ids, sims) if int(i) != photo_id]
    return {"photos": out[:limit]}


@router.post("/photos/{photo_id}/flags")
def set_flags(photo_id: int, favorite: bool | None = Body(None), hidden: bool | None = Body(None)):
    _visible_photo(get_state().conn(), photo_id)
    conn = get_state().conn()
    sets, args = [], []
    if favorite is not None:
        favorites.set_favorite(conn, [photo_id], favorite, current_user_id())
    if hidden is not None:
        sets.append("hidden=?")
        args.append(int(hidden))
    if sets:
        args.append(photo_id)
        conn.execute(f"UPDATE photos SET {', '.join(sets)} WHERE id=?", args)
    db.audit(conn, "photo_flags", "photo", photo_id, {"favorite": favorite, "hidden": hidden})
    conn.commit()
    if hidden is not None:
        from ..engine import visibility

        visibility.refresh(conn, [photo_id])
    return {"ok": True}


def _visible_photo(conn, photo_id: int):
    """404 for a missing photo, and for a locked one unless the Locked folder is open."""
    row = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "photo not found")
    guard_locked(row)
    return row


@router.post("/photos/{photo_id}/describe")
def describe_photo(photo_id: int, detailed: bool = False):
    """Generate (and cache) a description for one photo with the local vision model."""
    state = get_state()
    conn = state.conn()
    row = _visible_photo(conn, photo_id)
    if row["caption"] and not detailed:
        return {"caption": row["caption"], "cached": True}
    from ..vision.captioner import caption_photos

    try:
        caption_photos(state.ctx, conn, photo_ids=[photo_id], detailed=detailed)
    except Exception as exc:
        raise HTTPException(503, f"captioning unavailable: {exc}")
    caption = conn.execute("SELECT caption FROM photos WHERE id = ?", (photo_id,)).fetchone()["caption"]
    return {"caption": caption, "cached": False}


@router.get("/timeline")
def timeline(person: int | None = None, place: int | None = None, tag: str | None = None):
    """Year -> month buckets with counts and a cover photo, plus the events in each month."""
    state = get_state()
    conn = state.conn()
    where, args = photo_filter_sql(conn, [person] if person else None, place, None, None, None, tag,
                                   include_screenshots=False)
    rows = conn.execute(
        f"""SELECT strftime('%Y', p.taken_ts, 'unixepoch') AS y, strftime('%m', p.taken_ts, 'unixepoch') AS m,
                   COUNT(*) AS n, MAX(COALESCE(p.quality_score,0)) AS best
            FROM photos p WHERE {where} AND p.taken_ts IS NOT NULL GROUP BY y, m ORDER BY y DESC, m DESC""",
        args).fetchall()
    # One grouped query for every month's cover, rather than a query per month
    # (20 years of photos would otherwise mean 240 round trips).
    covers = {
        (c["y"], c["m"]): c["id"] for c in conn.execute(
            f"""SELECT y, m, id FROM (
                    SELECT strftime('%Y', p.taken_ts, 'unixepoch') AS y,
                           strftime('%m', p.taken_ts, 'unixepoch') AS m,
                           p.id AS id,
                           ROW_NUMBER() OVER (
                               PARTITION BY strftime('%Y', p.taken_ts, 'unixepoch'),
                                            strftime('%m', p.taken_ts, 'unixepoch')
                               ORDER BY COALESCE(p.quality_score, 0) DESC) AS rk
                    FROM photos p WHERE {where} AND p.taken_ts IS NOT NULL)
                WHERE rk = 1""", args)
    }
    years: dict[str, dict] = {}
    for r in rows:
        y = r["y"]
        years.setdefault(y, {"year": int(y), "count": 0, "months": []})
        years[y]["count"] += r["n"]
        years[y]["months"].append({"month": int(r["m"]), "count": r["n"],
                                   "cover_photo_id": covers.get((r["y"], r["m"]))})
    events = conn.execute(
        """SELECT id, kind, auto_title, user_title, start_ts, end_ts, photo_count, cover_photo_id, category,
                  parent_id FROM events ORDER BY start_ts DESC""").fetchall()
    ev = [{"id": e["id"], "kind": e["kind"], "title": event_title(e), "start_ts": e["start_ts"],
           "end_ts": e["end_ts"], "photo_count": e["photo_count"], "cover_photo_id": e["cover_photo_id"],
           "category": e["category"], "parent_id": e["parent_id"],
           "year": ts_to_naive(e["start_ts"]).year, "month": ts_to_naive(e["start_ts"]).month}
          for e in events]
    return {"years": sorted(years.values(), key=lambda x: -x["year"]), "events": ev}


@router.get("/stats")
def stats():
    state = get_state()
    conn = state.conn()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    # The headline numbers describe what you can see, so they leave out hidden photos (a phone's or
    # Google's trash, copies hidden from Duplicates). They used to count them, which is why the sidebar
    # said 29,500 photos while every page showed fewer. Disk usage (`bytes`) deliberately still counts
    # them: the files are on the disk whether or not they are shown.
    total = one("SELECT COUNT(*) FROM photos WHERE status='ok' AND hidden = 0 AND live_component = 0")
    date_row = conn.execute(
        "SELECT MIN(taken_ts), MAX(taken_ts) FROM photos "
        "WHERE status='ok' AND hidden = 0 AND live_component = 0 AND taken_ts IS NOT NULL").fetchone()
    top_places = [dict(r) for r in conn.execute(
        """SELECT pl.id, pl.name, pl.city, pl.admin1, pl.country, COUNT(p.id) n FROM places pl
           JOIN photos p ON p.place_id = pl.id WHERE p.status='ok' AND p.hidden = 0 AND p.live_component = 0
           GROUP BY pl.id ORDER BY n DESC LIMIT 8""")]
    return {
        "photos": total,
        # 'locked' and 'private' are left out: a count of photos someone cannot open is still news that they exist
        "photos_by_status": {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM photos WHERE status NOT IN ('locked', 'private') GROUP BY status")},
        "people": one("SELECT COUNT(*) FROM persons WHERE merged_into IS NULL AND ignored=0 AND face_count > 0"),
        "named_people": one("SELECT COUNT(*) FROM persons WHERE merged_into IS NULL AND name IS NOT NULL"),
        "faces": one("SELECT COUNT(*) FROM faces f JOIN photos p ON p.id = f.photo_id "
                     "WHERE p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0"),
        "events": one("SELECT COUNT(*) FROM events WHERE kind='event'"),
        "trips": one("SELECT COUNT(*) FROM events WHERE kind='trip'"),
        "albums": one(f"SELECT COUNT(*) FROM albums a WHERE a.hidden = 0 AND "
                      f"{albums_mod.visible_sql('a', current_user_id())}"),
        "videos": one("SELECT COUNT(*) FROM photos WHERE status='ok' AND hidden = 0 AND media_type='video' "
                      "AND live_component=0"),
        "places": one("SELECT COUNT(DISTINCT place_id) FROM photos WHERE place_id IS NOT NULL AND status='ok' "
                      "AND hidden = 0 AND live_component = 0"),
        "duplicate_groups": one("SELECT COUNT(*) FROM dup_groups g WHERE g.kind != 'similar' AND "
                                "(SELECT COUNT(*) FROM dup_members m JOIN photos p ON p.id = m.photo_id "
                                " WHERE m.group_id = g.id AND p.status NOT IN ('locked', 'private')) > 1"),
        "duplicate_photos": one("SELECT COUNT(DISTINCT m.photo_id) FROM dup_members m JOIN dup_groups g "
                                "ON g.id=m.group_id JOIN photos p ON p.id = m.photo_id "
                                "WHERE g.kind != 'similar' AND p.status NOT IN ('locked', 'private')"),
        "with_gps": one("SELECT COUNT(*) FROM photos WHERE gps_lat IS NOT NULL AND status='ok' AND hidden = 0 "
                        "AND live_component = 0"),
        "favorites": favorites.count(conn, current_user_id()),
        "errors": one("SELECT COUNT(*) FROM photos WHERE status='error'"),
        "missing": one("SELECT COUNT(*) FROM photos WHERE status='missing'"),
        "trash": one("SELECT COUNT(*) FROM photos WHERE status='trashed' AND live_component=0"),
        "pending": one("SELECT COUNT(*) FROM photos WHERE status='pending'"),
        "bytes": one("SELECT COALESCE(SUM(size),0) FROM photos WHERE status='ok'"),
        "date_range": {"from": date_row[0], "to": date_row[1]},
        "top_places": top_places,
        "roots": [dict(r) for r in conn.execute("SELECT id, path, last_scan_at FROM roots")],
    }


@router.get("/memories")
def memories(limit: int = 12):
    """Home screen: on this day, recent events, trips, rediscovered moments."""
    state = get_state()
    conn = state.conn()
    now = datetime.now()
    out: dict = {"sections": []}

    # On this day (any year)
    rows = conn.execute(
        """SELECT p.id, p.taken_ts FROM photos p
           WHERE p.status='ok' AND p.hidden=0 AND p.archived=0 AND p.live_component=0 AND p.taken_ts IS NOT NULL
             AND strftime('%m-%d', p.taken_ts, 'unixepoch') = ?
             AND COALESCE(p.source_kind,'') != 'screenshot'
           ORDER BY COALESCE(p.quality_score,0) DESC LIMIT 60""", (now.strftime("%m-%d"),)).fetchall()
    if rows:
        by_year: dict[int, list[int]] = {}
        for r in rows:
            by_year.setdefault(ts_to_naive(r["taken_ts"]).year, []).append(r["id"])
        out["sections"].append({
            "kind": "on_this_day", "title": "On this day",
            "subtitle": now.strftime("%d %B"),
            "groups": [{"title": str(y), "photo_ids": ids[:12]} for y, ids in sorted(by_year.items(), reverse=True)],
        })

    # Birthdays coming up (or today): that person's photos from around their past birthdays.
    for p in conn.execute(
            "SELECT id, name, display_no, birth_date, cover_face_id FROM persons "
            "WHERE birth_date IS NOT NULL AND merged_into IS NULL AND ignored = 0").fetchall():
        md = p["birth_date"][-5:]
        try:
            this_year = datetime(now.year, int(md[:2]), int(md[3:]))
        except ValueError:          # 29 February in a common year
            this_year = datetime(now.year, 3, 1)
        days_to = (this_year.date() - now.date()).days
        if not -1 <= days_to <= 7:
            continue
        by_year: dict[int, list[int]] = {}
        for r in conn.execute(
                """SELECT DISTINCT ph.id, ph.taken_ts FROM photos ph JOIN faces f ON f.photo_id = ph.id
                   WHERE f.person_id = ? AND ph.status = 'ok' AND ph.hidden = 0 AND ph.archived = 0 AND ph.live_component = 0
                     AND ph.taken_ts IS NOT NULL
                     AND ABS(julianday(strftime('%Y', ph.taken_ts, 'unixepoch') || '-' || ?) -
                             julianday(date(ph.taken_ts, 'unixepoch'))) <= 2
                   ORDER BY ph.rating DESC, COALESCE(ph.quality_score, 0) DESC""", (p["id"], md)):
            y = ts_to_naive(r["taken_ts"]).year
            if len(by_year.setdefault(y, [])) < 8:
                by_year[y].append(r["id"])
        if not by_year:
            continue
        # The age on *this* birthday, from the same clock the window was computed with.
        turning = this_year.year - int(p["birth_date"][:4]) if p["birth_date"][:1].isdigit() else None
        when = "today" if days_to == 0 else ("yesterday" if days_to == -1 else f"in {days_to} days")
        label = person_label(p)
        verb = "turned" if days_to < 0 else "turns"
        out["sections"].insert(0, {
            "kind": "birthday", "title": f"{label}'s birthday",
            "subtitle": (f"{verb} {turning} {when}" if turning is not None
                         else f"{when} · {this_year.strftime('%d %B')}"),
            "person_id": p["id"],
            "groups": [{"title": str(y), "photo_ids": ids} for y, ids in sorted(by_year.items(), reverse=True)],
        })

    # Recent events
    events = conn.execute(
        """SELECT * FROM events WHERE kind='event' AND photo_count >= 6 ORDER BY start_ts DESC LIMIT ?""",
        (limit,)).fetchall()
    if events:
        out["sections"].append({
            "kind": "recent_events", "title": "Recent events",
            "items": [{"id": e["id"], "title": event_title(e), "subtitle": date_range_label(e["start_ts"], e["end_ts"]),
                       "cover_photo_id": e["cover_photo_id"], "photo_count": e["photo_count"],
                       "category": e["category"]} for e in events],
        })

    trips = conn.execute("SELECT * FROM events WHERE kind='trip' ORDER BY start_ts DESC LIMIT 8").fetchall()
    if trips:
        out["sections"].append({
            "kind": "trips", "title": "Trips",
            "items": [{"id": e["id"], "title": event_title(e), "subtitle": date_range_label(e["start_ts"], e["end_ts"]),
                       "cover_photo_id": e["cover_photo_id"], "photo_count": e["photo_count"],
                       "category": "trip"} for e in trips],
        })

    people = conn.execute(
        """SELECT * FROM persons WHERE merged_into IS NULL AND ignored=0 AND photo_count > 0
           ORDER BY (name IS NULL), photo_count DESC LIMIT 12""").fetchall()
    if people:
        out["sections"].append({
            "kind": "people", "title": "People",
            "items": [{"id": p["id"], "title": person_label(p), "cover_face_id": p["cover_face_id"],
                       "photo_count": p["photo_count"]} for p in people],
        })

    years_ago = []
    for delta in (1, 2, 3, 5):
        y = now.year - delta
        row = conn.execute(
            """SELECT id FROM photos WHERE status='ok' AND hidden=0 AND archived=0 AND live_component=0 AND taken_ts IS NOT NULL
               AND strftime('%Y', taken_ts, 'unixepoch') = ? AND COALESCE(source_kind,'') != 'screenshot'
               ORDER BY rating DESC, COALESCE(quality_score,0) DESC LIMIT 8""", (str(y),)).fetchall()
        if row:
            years_ago.append({"title": f"{delta} year{'s' if delta > 1 else ''} ago", "year": y,
                              "photo_ids": [r[0] for r in row]})
    if years_ago:
        out["sections"].append({"kind": "years_ago", "title": "Rediscover", "groups": years_ago})
    return out


@router.get("/folders")
def folders():
    conn = get_state().conn()
    rows = conn.execute(
        "SELECT folder, COUNT(*) n FROM photos WHERE status='ok' GROUP BY folder ORDER BY n DESC LIMIT 400"
    ).fetchall()
    return {"folders": [{"path": r["folder"], "count": r["n"]} for r in rows]}
