"""People: gallery, person detail, and every manual correction operation."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..engine import people as people_mod
from ..engine.events import event_title
from ..engine.people import co_occurring, person_label
from ..metadata import ts_to_naive
from .deps import get_state

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/people")
def list_people(include_hidden: bool = False, include_ignored: bool = False, min_photos: int = 1,
                sort: str = Query("photos", pattern="^(photos|name|recent)$")):
    conn = get_state().conn()
    where = ["merged_into IS NULL", "photo_count >= ?"]
    args: list = [min_photos]
    if not include_hidden:
        where.append("hidden = 0")
    if not include_ignored:
        where.append("ignored = 0")
    order = {"photos": "(name IS NULL), photo_count DESC", "name": "(name IS NULL), name COLLATE NOCASE",
             "recent": "last_seen_ts DESC"}[sort]
    rows = conn.execute(f"SELECT * FROM persons WHERE {' AND '.join(where)} ORDER BY {order}", args).fetchall()
    people = [{
        "id": r["id"], "label": person_label(r), "name": r["name"], "display_no": r["display_no"],
        "photo_count": r["photo_count"], "face_count": r["face_count"], "cover_face_id": r["cover_face_id"],
        "first_seen_ts": r["first_seen_ts"], "last_seen_ts": r["last_seen_ts"],
        "confidence": round(r["cluster_confidence"], 3) if r["cluster_confidence"] else None,
        "hidden": bool(r["hidden"]), "ignored": bool(r["ignored"]), "named": bool(r["name"]),
    } for r in rows]
    unassigned = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE person_id IS NULL AND quality >= 0.3").fetchone()[0]
    return {"people": people, "unassigned_faces": unassigned,
            "me_person_id": get_state().ctx.settings.me_person_id}


@router.get("/people/{person_id}")
def person_detail(person_id: int, photo_limit: int = 500):
    state = get_state()
    conn = state.conn()
    r = conn.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "person not found")
    if r["merged_into"]:
        return person_detail(r["merged_into"], photo_limit)

    events = [{"id": e["id"], "title": event_title(e), "kind": e["kind"], "start_ts": e["start_ts"],
               "end_ts": e["end_ts"], "photo_count": e["photo_count"], "matched": e["n"],
               "cover_photo_id": e["cover_photo_id"], "category": e["category"]} for e in conn.execute(
        """SELECT e.*, COUNT(DISTINCT p.id) n FROM events e JOIN photos p ON p.event_id = e.id
           JOIN faces f ON f.photo_id = p.id WHERE f.person_id = ? AND e.kind='event'
           GROUP BY e.id ORDER BY e.start_ts DESC LIMIT 60""", (person_id,))]
    # Group by city: a person's summary should say "Hyderabad", not list six of its
    # neighbourhoods. The representative place id is the most-photographed one per city.
    place_rows = conn.execute(
        """SELECT pl.id, COALESCE(pl.city, pl.name) AS city, pl.admin1, pl.country, pl.lat, pl.lon,
                  COUNT(DISTINCT ph.id) n
           FROM places pl JOIN photos ph ON ph.place_id = pl.id
           JOIN faces f ON f.photo_id = ph.id WHERE f.person_id = ?
           GROUP BY pl.id ORDER BY n DESC""", (person_id,)).fetchall()
    by_city: dict[str, dict] = {}
    for pr in place_rows:
        key = f"{pr['city']}|{pr['admin1']}"
        hit = by_city.get(key)
        if hit is None:
            by_city[key] = {"id": pr["id"], "name": pr["city"], "city": pr["city"], "admin1": pr["admin1"],
                            "country": pr["country"], "count": pr["n"], "lat": pr["lat"], "lon": pr["lon"]}
        else:
            hit["count"] += pr["n"]
    places = sorted(by_city.values(), key=lambda x: -x["count"])[:12]
    years = [{"year": int(y["y"]), "count": y["n"]} for y in conn.execute(
        """SELECT strftime('%Y', ph.taken_ts, 'unixepoch') y, COUNT(DISTINCT ph.id) n
           FROM photos ph JOIN faces f ON f.photo_id = ph.id
           WHERE f.person_id = ? AND ph.taken_ts IS NOT NULL GROUP BY y ORDER BY y""", (person_id,))]
    best = [int(x[0]) for x in conn.execute(
        """SELECT DISTINCT ph.id FROM photos ph JOIN faces f ON f.photo_id = ph.id
           WHERE f.person_id = ? AND ph.status='ok'
           ORDER BY (COALESCE(ph.quality_score,0) + f.quality * 20) DESC LIMIT 12""", (person_id,))]
    return {
        "id": r["id"], "label": person_label(r), "name": r["name"], "display_no": r["display_no"],
        "photo_count": r["photo_count"], "face_count": r["face_count"], "cover_face_id": r["cover_face_id"],
        "first_seen_ts": r["first_seen_ts"], "last_seen_ts": r["last_seen_ts"],
        "confidence": round(r["cluster_confidence"], 3) if r["cluster_confidence"] else None,
        "hidden": bool(r["hidden"]), "ignored": bool(r["ignored"]),
        "events": events, "places": places, "years": years, "representative_photos": best,
        "co_occurring": co_occurring(conn, person_id),
        "is_me": state.ctx.settings.me_person_id == person_id,
    }


@router.get("/people/{person_id}/faces")
def person_faces(person_id: int, limit: int = Query(300, le=2000), offset: int = 0,
                 order: str = Query("confidence", pattern="^(confidence|quality|recent)$")):
    """Faces for review — least-confident first by default so mistakes surface."""
    conn = get_state().conn()
    order_sql = {"confidence": "COALESCE(f.assign_confidence, 0) ASC, f.quality ASC",
                 "quality": "f.quality DESC", "recent": "p.taken_ts DESC"}[order]
    rows = conn.execute(
        f"""SELECT f.id, f.photo_id, f.assign_confidence, f.assign_source, f.quality, f.det_score,
                   f.x1, f.y1, f.x2, f.y2, p.taken_ts
            FROM faces f JOIN photos p ON p.id = f.photo_id
            WHERE f.person_id = ? ORDER BY {order_sql} LIMIT ? OFFSET ?""",
        (person_id, limit, offset)).fetchall()
    return {"faces": [{"id": r["id"], "photo_id": r["photo_id"], "confidence": r["assign_confidence"],
                       "source": r["assign_source"], "quality": round(r["quality"], 3),
                       "box": [r["x1"], r["y1"], r["x2"], r["y2"]], "taken_ts": r["taken_ts"]} for r in rows]}


@router.get("/faces/unassigned")
def unassigned_faces(limit: int = Query(200, le=1000), min_quality: float = 0.35):
    conn = get_state().conn()
    rows = conn.execute(
        """SELECT f.id, f.photo_id, f.quality, f.x1, f.y1, f.x2, f.y2, p.taken_ts FROM faces f
           JOIN photos p ON p.id = f.photo_id
           WHERE f.person_id IS NULL AND f.quality >= ? AND p.status='ok'
           ORDER BY f.quality DESC LIMIT ?""", (min_quality, limit)).fetchall()
    return {"faces": [{"id": r["id"], "photo_id": r["photo_id"], "quality": round(r["quality"], 3),
                       "box": [r["x1"], r["y1"], r["x2"], r["y2"]], "taken_ts": r["taken_ts"]} for r in rows]}


class RenameBody(BaseModel):
    name: str | None = None


@router.post("/people/{person_id}/rename")
def rename(person_id: int, body: RenameBody):
    conn = get_state().conn()
    people_mod.rename_person(conn, person_id, (body.name or "").strip() or None)
    db.bump_generation(conn, "people")
    conn.commit()
    return {"ok": True, "id": person_id, "name": body.name}


class FlagsBody(BaseModel):
    hidden: bool | None = None
    ignored: bool | None = None
    is_me: bool | None = None


@router.post("/people/{person_id}/flags")
def flags(person_id: int, body: FlagsBody):
    state = get_state()
    conn = state.conn()
    people_mod.set_person_flags(conn, person_id, body.hidden, body.ignored)
    if body.is_me is not None:
        state.ctx.settings.me_person_id = person_id if body.is_me else None
        state.ctx.settings.save(state.ctx.paths.data)
    db.bump_generation(conn, "people")
    conn.commit()
    return {"ok": True}


class MergeBody(BaseModel):
    target_id: int
    source_ids: list[int]


@router.post("/people/merge")
def merge(body: MergeBody):
    conn = get_state().conn()
    return people_mod.merge_persons(conn, body.target_id, body.source_ids)


class SplitBody(BaseModel):
    face_ids: list[int]
    name: str | None = None


@router.post("/people/{person_id}/split")
def split(person_id: int, body: SplitBody):
    conn = get_state().conn()
    if not body.face_ids:
        raise HTTPException(400, "no faces given")
    return people_mod.split_person(conn, person_id, body.face_ids, body.name)


class AssignBody(BaseModel):
    face_ids: list[int]
    person_id: int | None = None
    name: str | None = None


@router.post("/faces/assign")
def assign(body: AssignBody):
    conn = get_state().conn()
    if not body.face_ids:
        raise HTTPException(400, "no faces given")
    return people_mod.assign_faces(conn, body.face_ids, body.person_id, body.name)


class RejectBody(BaseModel):
    face_ids: list[int]
    person_id: int


@router.post("/faces/reject")
def reject(body: RejectBody):
    conn = get_state().conn()
    return people_mod.reject_faces(conn, body.face_ids, body.person_id)


@router.get("/people/suggestions/merges")
def merge_suggestions(limit: int = 20):
    state = get_state()
    return {"suggestions": people_mod.merge_suggestions(state.ctx, state.conn(), limit=limit)}


class NotSameBody(BaseModel):
    a: int
    b: int


@router.post("/people/not-same")
def not_same(body: NotSameBody):
    conn = get_state().conn()
    people_mod.mark_not_same(conn, body.a, body.b)
    return {"ok": True}
