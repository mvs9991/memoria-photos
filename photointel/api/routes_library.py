"""Photos, timeline, memories and library statistics."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, HTTPException, Query

from .. import db
from ..engine.events import date_range_label, event_title
from ..engine.people import person_label
from ..engine.places import place_label
from ..metadata import ts_to_naive
from .deps import get_state

log = logging.getLogger(__name__)
router = APIRouter()


def photo_filter_sql(conn, person: list[int] | None = None, place: int | None = None, event: int | None = None,
                     year: int | None = None, month: int | None = None, tag: str | None = None,
                     source: str | None = None, favorite: bool = False, camera: str | None = None,
                     folder: str | None = None, has_faces: bool | None = None,
                     include_screenshots: bool = True) -> tuple[str, list]:
    where = ["p.status = 'ok'", "p.hidden = 0"]
    args: list = []
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
    if folder:
        where.append("(p.folder = ? OR p.folder LIKE ?)")
        args.extend([folder, folder + "/%"])
    if favorite:
        where.append("p.favorite = 1")
    if has_faces is True:
        where.append("p.face_count > 0")
    return " AND ".join(where), args


@router.get("/photos/index")
def photos_index(person: list[int] = Query(default=[]), place: int | None = None, event: int | None = None,
                 year: int | None = None, month: int | None = None, tag: str | None = None,
                 source: str | None = None, favorite: bool = False, camera: str | None = None,
                 folder: str | None = None, has_faces: bool | None = None, include_screenshots: bool = True,
                 order: str = Query("date_desc", pattern="^(date_desc|date_asc|quality)$"),
                 limit: int = Query(200000, le=500000)):
    """Columnar photo list for the virtualised grid: ids, aspect ratios, timestamps.

    Compact on purpose — a 100k-photo library is ~1 MB of JSON (far less gzipped),
    which lets the client lay out and scrub the whole timeline without paging.
    """
    state = get_state()
    conn = state.conn()
    where, args = photo_filter_sql(conn, person, place, event, year, month, tag, source, favorite, camera,
                                   folder, has_faces, include_screenshots)
    order_sql = {"date_desc": "p.taken_ts DESC, p.id DESC", "date_asc": "p.taken_ts ASC, p.id ASC",
                 "quality": "COALESCE(p.quality_score,0) DESC"}[order]
    rows = conn.execute(
        f"SELECT p.id, p.width, p.height, p.taken_ts, p.face_count, p.favorite FROM photos p "
        f"WHERE {where} ORDER BY {order_sql} LIMIT ?", (*args, limit)).fetchall()
    ids, ratios, ts, flags = [], [], [], []
    for r in rows:
        ids.append(r["id"])
        w, h = r["width"] or 4, r["height"] or 3
        ratios.append(round(max(0.2, min(6.0, w / max(h, 1))), 3))
        ts.append(int(r["taken_ts"] or 0))
        flags.append((1 if r["favorite"] else 0) | (2 if (r["face_count"] or 0) > 0 else 0))
    return {"ids": ids, "ratio": ratios, "ts": ts, "flags": flags, "total": len(ids)}


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
    faces = []
    for f in conn.execute(
            """SELECT f.*, pe.name, pe.display_no, pe.id AS pid FROM faces f
               LEFT JOIN persons pe ON pe.id = f.person_id WHERE f.photo_id = ? ORDER BY f.size_px DESC""",
            (photo_id,)):
        faces.append({
            "id": f["id"], "box": [f["x1"], f["y1"], f["x2"], f["y2"]], "person_id": f["pid"],
            "label": person_label(f) if f["pid"] else None, "confidence": f["assign_confidence"],
            "assign_source": f["assign_source"], "quality": f["quality"], "det_score": f["det_score"],
        })
    from ..engine.tags import confidence as _tag_conf

    tags = [{"name": t["name"], "category": t["category"], "score": round(t["score"], 3),
             "confidence": round(_tag_conf(t["score"]), 3)} for t in conn.execute(
        """SELECT t.name, t.category, pt.score FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
           WHERE pt.photo_id = ? ORDER BY pt.score DESC LIMIT 12""", (photo_id,))]
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
        "root": row["root"], "ext": row["ext"], "size": row["size"], "width": row["width"], "height": row["height"],
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
        "favorite": bool(row["favorite"]), "hidden": bool(row["hidden"]),
        "event": {"id": row["event_id2"], "title": event_title(row) if row["event_id2"] else None,
                  "kind": row["event_kind"]} if row["event_id2"] else None,
        "sha256": row["sha256"],
    }


@router.get("/photos/{photo_id}/similar")
def similar_photos(photo_id: int, limit: int = Query(24, le=100)):
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
    conn = get_state().conn()
    sets, args = [], []
    if favorite is not None:
        sets.append("favorite=?")
        args.append(int(favorite))
    if hidden is not None:
        sets.append("hidden=?")
        args.append(int(hidden))
    if not sets:
        return {"ok": True}
    args.append(photo_id)
    conn.execute(f"UPDATE photos SET {', '.join(sets)} WHERE id=?", args)
    db.audit(conn, "photo_flags", "photo", photo_id, {"favorite": favorite, "hidden": hidden})
    conn.commit()
    return {"ok": True}


@router.post("/photos/{photo_id}/describe")
def describe_photo(photo_id: int, detailed: bool = False):
    """Generate (and cache) a description for one photo with the local vision model."""
    state = get_state()
    conn = state.conn()
    row = conn.execute("SELECT caption FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "photo not found")
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
    total = one("SELECT COUNT(*) FROM photos WHERE status='ok'")
    date_row = conn.execute(
        "SELECT MIN(taken_ts), MAX(taken_ts) FROM photos WHERE status='ok' AND taken_ts IS NOT NULL").fetchone()
    top_places = [dict(r) for r in conn.execute(
        """SELECT pl.id, pl.name, pl.city, pl.admin1, pl.country, COUNT(p.id) n FROM places pl
           JOIN photos p ON p.place_id = pl.id WHERE p.status='ok' GROUP BY pl.id ORDER BY n DESC LIMIT 8""")]
    return {
        "photos": total,
        "photos_by_status": {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM photos GROUP BY status")},
        "people": one("SELECT COUNT(*) FROM persons WHERE merged_into IS NULL AND ignored=0 AND face_count > 0"),
        "named_people": one("SELECT COUNT(*) FROM persons WHERE merged_into IS NULL AND name IS NOT NULL"),
        "faces": one("SELECT COUNT(*) FROM faces"),
        "events": one("SELECT COUNT(*) FROM events WHERE kind='event'"),
        "trips": one("SELECT COUNT(*) FROM events WHERE kind='trip'"),
        "places": one("SELECT COUNT(DISTINCT place_id) FROM photos WHERE place_id IS NOT NULL"),
        "duplicate_groups": one("SELECT COUNT(*) FROM dup_groups WHERE kind != 'similar'"),
        "duplicate_photos": one("SELECT COUNT(DISTINCT photo_id) FROM dup_members m JOIN dup_groups g "
                                "ON g.id=m.group_id WHERE g.kind != 'similar'"),
        "with_gps": one("SELECT COUNT(*) FROM photos WHERE gps_lat IS NOT NULL AND status='ok'"),
        "favorites": one("SELECT COUNT(*) FROM photos WHERE favorite=1"),
        "errors": one("SELECT COUNT(*) FROM photos WHERE status='error'"),
        "missing": one("SELECT COUNT(*) FROM photos WHERE status='missing'"),
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
           WHERE p.status='ok' AND p.hidden=0 AND p.taken_ts IS NOT NULL
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
            """SELECT id FROM photos WHERE status='ok' AND hidden=0 AND taken_ts IS NOT NULL
               AND strftime('%Y', taken_ts, 'unixepoch') = ? AND COALESCE(source_kind,'') != 'screenshot'
               ORDER BY COALESCE(quality_score,0) DESC LIMIT 8""", (str(y),)).fetchall()
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
