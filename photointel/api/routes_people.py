"""People: gallery, person detail, and every manual correction operation."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..engine import people as people_mod
from ..engine.events import event_title
from ..engine.people import co_occurring, person_label
from ..metadata import ts_to_naive
from .deps import current_role, get_state, visible_ids
from .routes_events import _visible_cover

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/people")
def list_people(include_hidden: bool = False, include_ignored: bool = False, min_photos: int = 1,
                sort: str = Query("photos", pattern="^(photos|name|recent)$")):
    conn = get_state().conn()
    where = ["merged_into IS NULL", "photo_count >= ?"]
    # Anyone but an owner sees only people with a photo they can see: someone who appears only in locked or
    # private photos was still listed (and named) to the family with ?min_photos=0.
    args: list = [min_photos if current_role() == "owner" else max(min_photos, 1)]
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
        "SELECT COUNT(*) FROM faces f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.person_id IS NULL AND f.quality >= 0.3 AND p.status = 'ok' AND p.hidden = 0 "
        "AND p.live_component = 0").fetchone()[0]
    return {"people": people, "unassigned_faces": unassigned,
            "me_person_id": _me(conn)}


@router.get("/people/{person_id}")
def person_detail(person_id: int, photo_limit: int = 500):
    state = get_state()
    conn = state.conn()
    r = conn.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "person not found")
    # A merged person shows the one it was merged into, following the chain. Iteratively, and never round a
    # loop: a merge made from a stale page once pointed two people at each other, and this recursed forever.
    seen = {person_id}
    while r["merged_into"]:
        if r["merged_into"] in seen:
            break
        seen.add(r["merged_into"])
        nxt = conn.execute("SELECT * FROM persons WHERE id=?", (r["merged_into"],)).fetchone()
        if nxt is None:
            break
        r = nxt
    person_id = r["id"]
    if r["photo_count"] == 0 and current_role() != "owner":
        raise HTTPException(404, "person not found")    # only in photos this account cannot see

    events = [{"id": e["id"], "title": event_title(e), "kind": e["kind"], "start_ts": e["start_ts"],
               "end_ts": e["end_ts"], "photo_count": e["photo_count"], "matched": e["n"],
               "cover_photo_id": _visible_cover(conn, e), "category": e["category"]} for e in conn.execute(
        """SELECT e.*, COUNT(DISTINCT p.id) n FROM events e JOIN photos p ON p.event_id = e.id
           JOIN faces f ON f.photo_id = p.id WHERE f.person_id = ? AND e.kind='event'
             AND p.status = 'ok' AND p.hidden = 0
           GROUP BY e.id ORDER BY e.start_ts DESC LIMIT 60""", (person_id,))]
    # Group by city: a person's summary should say "Hyderabad", not list six of its
    # neighbourhoods. The representative place id is the most-photographed one per city.
    place_rows = conn.execute(
        """SELECT pl.id, COALESCE(pl.city, pl.name) AS city, pl.admin1, pl.country, pl.lat, pl.lon,
                  COUNT(DISTINCT ph.id) n
           FROM places pl JOIN photos ph ON ph.place_id = pl.id
           JOIN faces f ON f.photo_id = ph.id WHERE f.person_id = ?
             AND ph.status = 'ok' AND ph.hidden = 0
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
           WHERE f.person_id = ? AND ph.taken_ts IS NOT NULL AND ph.status = 'ok' AND ph.hidden = 0
           GROUP BY y ORDER BY y""", (person_id,))]
    best = [int(x[0]) for x in conn.execute(
        """SELECT DISTINCT ph.id FROM photos ph JOIN faces f ON f.photo_id = ph.id
           WHERE f.person_id = ? AND ph.status='ok' AND ph.hidden = 0 AND ph.live_component = 0
           ORDER BY (COALESCE(ph.quality_score,0) + f.quality * 20) DESC LIMIT 12""", (person_id,))]
    # The page lists a dozen events; say how many there are, not how many were sent (it said 60 at most).
    event_count = conn.execute(
        """SELECT COUNT(DISTINCT p.event_id) FROM photos p JOIN faces f ON f.photo_id = p.id
           JOIN events e ON e.id = p.event_id WHERE f.person_id = ? AND e.kind = 'event'
             AND p.status = 'ok' AND p.hidden = 0""", (person_id,)).fetchone()[0]
    return {
        "id": r["id"], "label": person_label(r), "name": r["name"], "display_no": r["display_no"],
        "photo_count": r["photo_count"], "face_count": r["face_count"], "cover_face_id": r["cover_face_id"],
        "first_seen_ts": r["first_seen_ts"], "last_seen_ts": r["last_seen_ts"],
        "confidence": round(r["cluster_confidence"], 3) if r["cluster_confidence"] else None,
        "hidden": bool(r["hidden"]), "ignored": bool(r["ignored"]),
        "events": events, "event_count": event_count, "places": places, "years": years, "representative_photos": best,
        "co_occurring": co_occurring(conn, person_id),
        "is_me": _me(conn) == person_id,
        "birth_date": r["birth_date"],
        "age": people_mod.age_on(r["birth_date"], None),
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
            WHERE f.person_id = ? AND p.status = 'ok' AND p.hidden = 0 ORDER BY {order_sql} LIMIT ? OFFSET ?""",
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
           WHERE f.person_id IS NULL AND f.quality >= ? AND p.status='ok' AND p.hidden = 0 AND p.live_component=0
           ORDER BY f.quality DESC LIMIT ?""", (min_quality, limit)).fetchall()
    return {"faces": [{"id": r["id"], "photo_id": r["photo_id"], "quality": round(r["quality"], 3),
                       "box": [r["x1"], r["y1"], r["x2"], r["y2"]], "taken_ts": r["taken_ts"]} for r in rows]}


def _person_or_404(conn, person_id: int) -> int:
    """The person (or the one it was merged into), or a 404. An id that does not exist reached the engine,
    failed on a foreign key with its write transaction still open, and every other request then waited out
    the 60 s lock timeout."""
    if conn.execute("SELECT 1 FROM persons WHERE id = ?", (int(person_id),)).fetchone() is None:
        raise HTTPException(404, "person not found")
    return people_mod.live_person(conn, int(person_id))


def _me(conn) -> int | None:
    """Who "me" is now: the setting keeps the id chosen, which a merge may have folded into another person."""
    me = get_state().ctx.settings.me_person_id
    return people_mod.live_person(conn, me) if me else None


class RenameBody(BaseModel):
    name: str | None = None


@router.post("/people/{person_id}/rename")
def rename(person_id: int, body: RenameBody):
    conn = get_state().conn()
    person_id = _person_or_404(conn, person_id)
    name = (body.name or "").strip() or None
    people_mod.rename_person(conn, person_id, name)
    db.bump_generation(conn, "people")
    conn.commit()
    return {"ok": True, "id": person_id, "name": name}


class FlagsBody(BaseModel):
    hidden: bool | None = None
    ignored: bool | None = None
    is_me: bool | None = None
    birth_date: str | None = None     # 'YYYY-MM-DD', '--MM-DD' (year unknown) or '' to clear


@router.post("/people/{person_id}/flags")
def flags(person_id: int, body: FlagsBody):
    state = get_state()
    conn = state.conn()
    if body.is_me is not None and current_role() != "owner":
        # "Me" is the library's setting (settings.json), which only an owner may change.
        raise HTTPException(403, "only an owner can choose who 'me' is")
    person_id = _person_or_404(conn, person_id)
    people_mod.set_person_flags(conn, person_id, body.hidden, body.ignored)
    if body.birth_date is not None:
        try:
            people_mod.set_birth_date(conn, person_id, body.birth_date)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if body.is_me is not None:
        state.ctx.settings.me_person_id = person_id if body.is_me else (
            None if _me(conn) == person_id else state.ctx.settings.me_person_id)
        state.ctx.settings.save(state.ctx.paths.data)
    db.bump_generation(conn, "people")
    conn.commit()
    return {"ok": True}


class HideManyBody(BaseModel):
    ids: list[int] = Field(max_length=20000)
    hidden: bool = True


@router.post("/people/hide")
def hide_many(body: HideManyBody):
    """Hide or unhide a selection of people in one go (the People page's bulk action)."""
    return {"changed": people_mod.set_people_hidden(get_state().conn(), body.ids, body.hidden)}


class MergeBody(BaseModel):
    target_id: int
    source_ids: list[int]


@router.post("/people/merge")
def merge(body: MergeBody):
    conn = get_state().conn()
    _person_or_404(conn, body.target_id)
    sources = [s for s in body.source_ids if conn.execute("SELECT 1 FROM persons WHERE id = ?", (int(s),)).fetchone()]
    if not sources:
        raise HTTPException(404, "person not found")
    return people_mod.merge_persons(conn, body.target_id, sources)


class SplitBody(BaseModel):
    face_ids: list[int]
    name: str | None = None


@router.post("/people/{person_id}/split")
def split(person_id: int, body: SplitBody):
    conn = get_state().conn()
    if not body.face_ids:
        raise HTTPException(400, "no faces given")
    person_id = _person_or_404(conn, person_id)
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
    if body.person_id is not None:
        _person_or_404(conn, body.person_id)
    face_ids = _visible_faces(conn, body.face_ids)
    if not face_ids:
        return {"person_id": body.person_id, "faces": 0}
    return people_mod.assign_faces(conn, face_ids, body.person_id, body.name)


class RejectBody(BaseModel):
    face_ids: list[int]
    person_id: int


@router.post("/faces/reject")
def reject(body: RejectBody):
    conn = get_state().conn()
    person_id = _person_or_404(conn, body.person_id)
    return people_mod.reject_faces(conn, _visible_faces(conn, body.face_ids), person_id)


def _visible_faces(conn, face_ids: list[int]) -> list[int]:
    """Faces on photos this caller may see (a family member could name a face on a locked photo)."""
    photo_of = {}
    for chunk, marks in db.chunks(face_ids):
        photo_of.update({int(r[0]): int(r[1]) for r in conn.execute(
            f"SELECT id, photo_id FROM faces WHERE id IN ({marks})", chunk)})
    ok = set(visible_ids(conn, list(photo_of.values())))
    return [f for f in dict.fromkeys(face_ids) if photo_of.get(f) in ok]


@router.get("/people/suggestions/merges")
def merge_suggestions(limit: int = 20):
    state = get_state()
    return {"suggestions": people_mod.merge_suggestions(state.ctx, state.conn(), limit=limit)}


@router.get("/people/suggestions/names")
def name_suggestions():
    """Names Google Photos used for people Memoria found (from Takeout sidecars). Never applied
    automatically: each comes with its evidence for the user to accept or dismiss."""
    from ..engine.takeout import name_suggestions as suggest

    return {"suggestions": suggest(get_state().conn())}


class NameDismissBody(BaseModel):
    person_id: int
    name: str


@router.post("/people/suggestions/names/dismiss")
def dismiss_name(body: NameDismissBody):
    from ..engine.takeout import dismiss_name_suggestion

    dismiss_name_suggestion(get_state().conn(), body.person_id, body.name)
    return {"ok": True}


class NotSameBody(BaseModel):
    a: int
    b: int


@router.post("/people/not-same")
def not_same(body: NotSameBody):
    conn = get_state().conn()
    people_mod.mark_not_same(conn, body.a, body.b)
    return {"ok": True}
