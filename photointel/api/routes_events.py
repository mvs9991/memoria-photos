"""Events, trips and places."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..engine.events import build_summary, date_range_label, event_title
from ..engine.people import person_label
from ..engine.places import place_label
from ..metadata import ts_to_naive
from .deps import get_state

router = APIRouter()


def _event_dict(conn, e, with_places: bool = False) -> dict:
    place = conn.execute("SELECT * FROM places WHERE id=?", (e["place_id"],)).fetchone() if e["place_id"] else None
    out = {
        "id": e["id"], "kind": e["kind"], "title": event_title(e), "auto_title": e["auto_title"],
        "user_title": e["user_title"], "category": e["category"], "start_ts": e["start_ts"], "end_ts": e["end_ts"],
        "date_label": date_range_label(e["start_ts"], e["end_ts"]), "photo_count": e["photo_count"],
        "people_count": e["people_count"], "cover_photo_id": e["cover_photo_id"], "summary": e["summary"],
        "place": {"id": place["id"], "label": place_label(place), "city": place["city"],
                  "country": place["country"], "lat": place["lat"], "lon": place["lon"]} if place is not None else None,
        "location_confidence": e["location_confidence"], "parent_id": e["parent_id"],
        "lat": e["lat"], "lon": e["lon"], "year": ts_to_naive(e["start_ts"]).year,
    }
    return out


@router.get("/events")
def list_events(kind: str | None = Query(None, pattern="^(event|trip)$"), year: int | None = None,
                person: int | None = None, place: int | None = None, category: str | None = None,
                limit: int = Query(500, le=2000)):
    conn = get_state().conn()
    where, args = ["1=1"], []
    if kind:
        where.append("e.kind = ?")
        args.append(kind)
    if year:
        where.append("strftime('%Y', e.start_ts, 'unixepoch') = ?")
        args.append(str(year))
    if category:
        where.append("e.category = ?")
        args.append(category)
    if place:
        where.append("e.place_id = ?")
        args.append(place)
    if person:
        where.append("""EXISTS (SELECT 1 FROM faces f JOIN photos p ON p.id = f.photo_id
                        WHERE f.person_id = ? AND (p.event_id = e.id OR EXISTS
                          (SELECT 1 FROM trip_photos tp WHERE tp.trip_id = e.id AND tp.photo_id = p.id)))""")
        args.append(person)
    rows = conn.execute(
        f"SELECT e.* FROM events e WHERE {' AND '.join(where)} ORDER BY e.start_ts DESC LIMIT ?",
        (*args, limit)).fetchall()
    return {"events": [_event_dict(conn, e) for e in rows]}


@router.get("/events/{event_id}")
def event_detail(event_id: int):
    conn = get_state().conn()
    e = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if e is None:
        raise HTTPException(404, "event not found")
    data = _event_dict(conn, e)
    # Rebuild rather than serve the stored text: people may have been renamed since.
    if e["summary_source"] != "llm":
        fresh = build_summary(conn, event_id)
        if fresh:
            data["summary"] = fresh
            if fresh != e["summary"]:
                conn.execute("UPDATE events SET summary=? WHERE id=?", (fresh, event_id))
                conn.commit()
    if e["kind"] == "trip":
        photo_sql = ("SELECT p.id, p.width, p.height, p.taken_ts FROM trip_photos tp JOIN photos p ON p.id=tp.photo_id "
                     "WHERE tp.trip_id=? AND p.status='ok' ORDER BY p.taken_ts")
        children = [_event_dict(conn, c) for c in conn.execute(
            "SELECT * FROM events WHERE parent_id=? ORDER BY start_ts", (event_id,))]
        data["children"] = children
    else:
        photo_sql = "SELECT id, width, height, taken_ts FROM photos WHERE event_id=? AND status='ok' ORDER BY taken_ts"
        data["children"] = []
    rows = conn.execute(photo_sql, (event_id,)).fetchall()
    data["photos"] = {"ids": [r["id"] for r in rows],
                      "ratio": [round(max(0.2, min(6.0, (r["width"] or 4) / max(r["height"] or 3, 1))), 3) for r in rows],
                      "ts": [int(r["taken_ts"] or 0) for r in rows]}
    ids = [r["id"] for r in rows][:900]
    if ids:
        marks = ",".join("?" * len(ids))
        data["people"] = [{"id": p["id"], "label": person_label(p), "cover_face_id": p["cover_face_id"],
                           "count": p["n"]} for p in conn.execute(
            f"""SELECT pe.*, COUNT(DISTINCT f.photo_id) n FROM persons pe JOIN faces f ON f.person_id = pe.id
                WHERE f.photo_id IN ({marks}) AND pe.merged_into IS NULL AND pe.ignored = 0
                GROUP BY pe.id ORDER BY n DESC LIMIT 20""", ids)]
        data["places"] = [{"id": p["id"], "label": place_label(p), "count": p["n"], "lat": p["lat"], "lon": p["lon"]}
                          for p in conn.execute(
            f"""SELECT pl.*, COUNT(ph.id) n FROM places pl JOIN photos ph ON ph.place_id = pl.id
                WHERE ph.id IN ({marks}) GROUP BY pl.id ORDER BY n DESC LIMIT 10""", ids)]
        data["tags"] = [{"name": t["name"], "score": round(t["s"], 3)} for t in conn.execute(
            f"""SELECT t.name, AVG(pt.score) s, COUNT(*) n FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
                WHERE pt.photo_id IN ({marks}) AND pt.score >= 2.5
                  AND t.category IN ('scene','activity','object')
                GROUP BY t.id HAVING COUNT(*) >= 2
                ORDER BY (COUNT(*) * AVG(pt.score)) DESC LIMIT 8""", ids)]
        data["highlights"] = [int(x[0]) for x in conn.execute(
            f"""SELECT id FROM photos WHERE id IN ({marks}) ORDER BY COALESCE(quality_score,0) DESC LIMIT 10""", ids)]
        gps = conn.execute(
            f"SELECT gps_lat, gps_lon FROM photos WHERE id IN ({marks}) AND gps_lat IS NOT NULL LIMIT 400", ids
        ).fetchall()
        data["map_points"] = [{"lat": g["gps_lat"], "lon": g["gps_lon"]} for g in gps]
    else:
        data.update({"people": [], "places": [], "tags": [], "highlights": [], "map_points": []})
    return data


class EventUpdate(BaseModel):
    title: str | None = None


@router.post("/events/{event_id}")
def update_event(event_id: int, body: EventUpdate):
    conn = get_state().conn()
    title = (body.title or "").strip() or None
    conn.execute("UPDATE events SET user_title=?, updated_at=strftime('%s','now') WHERE id=?", (title, event_id))
    db.audit(conn, "event_renamed", "event", event_id, {"title": title})
    conn.commit()
    db.bump_generation(conn, "events")
    conn.commit()
    return {"ok": True}


@router.post("/events/{event_id}/resummarise")
def resummarise(event_id: int, use_llm: bool = False):
    state = get_state()
    conn = state.conn()
    if use_llm and state.ctx.settings.llm_enabled:
        from ..search.llm import llm_event_summary

        summary = llm_event_summary(state.ctx, conn, event_id)
        source = "llm"
    else:
        summary = build_summary(conn, event_id)
        source = "template"
    conn.execute("UPDATE events SET summary=?, summary_source=? WHERE id=?", (summary, source, event_id))
    conn.commit()
    return {"summary": summary, "source": source}


# ----------------------------------------------------------------------------- places

@router.get("/places")
def list_places():
    """Hierarchy country -> region -> place with counts and covers."""
    conn = get_state().conn()
    rows = conn.execute(
        """SELECT pl.id, pl.name, pl.city, pl.admin1, pl.admin2, pl.country, pl.country_code, pl.lat, pl.lon,
                  COUNT(p.id) n, MAX(p.taken_ts) last_ts
           FROM places pl JOIN photos p ON p.place_id = pl.id
           WHERE p.status='ok' GROUP BY pl.id ORDER BY n DESC""").fetchall()
    places = []
    for r in rows:
        cover = conn.execute(
            "SELECT id FROM photos WHERE place_id=? AND status='ok' ORDER BY COALESCE(quality_score,0) DESC LIMIT 1",
            (r["id"],)).fetchone()
        events = conn.execute("SELECT COUNT(*) FROM events WHERE place_id=?", (r["id"],)).fetchone()[0]
        people = conn.execute(
            """SELECT COUNT(DISTINCT f.person_id) FROM faces f JOIN photos p ON p.id = f.photo_id
               WHERE p.place_id = ? AND f.person_id IS NOT NULL""", (r["id"],)).fetchone()[0]
        places.append({"id": r["id"], "name": r["name"], "city": r["city"], "admin1": r["admin1"],
                       "admin2": r["admin2"], "country": r["country"], "country_code": r["country_code"],
                       "lat": r["lat"], "lon": r["lon"], "photo_count": r["n"], "event_count": events,
                       "people_count": people, "cover_photo_id": cover[0] if cover else None,
                       "last_ts": r["last_ts"]})
    countries: dict[str, dict] = {}
    for p in places:
        c = countries.setdefault(p["country"] or "Unknown", {"country": p["country"] or "Unknown", "count": 0,
                                                             "regions": {}})
        c["count"] += p["photo_count"]
        reg = c["regions"].setdefault(p["admin1"] or "—", {"region": p["admin1"] or "—", "count": 0, "places": []})
        reg["count"] += p["photo_count"]
        reg["places"].append(p)
    hierarchy = [{"country": c["country"], "count": c["count"],
                  "regions": sorted(c["regions"].values(), key=lambda r: -r["count"])}
                 for c in sorted(countries.values(), key=lambda c: -c["count"])]
    return {"places": places, "hierarchy": hierarchy}


@router.get("/places/{place_id}")
def place_detail(place_id: int):
    conn = get_state().conn()
    p = conn.execute("SELECT * FROM places WHERE id=?", (place_id,)).fetchone()
    if p is None:
        raise HTTPException(404, "place not found")
    events = [_event_dict(conn, e) for e in conn.execute(
        "SELECT * FROM events WHERE place_id=? ORDER BY start_ts DESC LIMIT 100", (place_id,))]
    people = [{"id": r["id"], "label": person_label(r), "cover_face_id": r["cover_face_id"], "count": r["n"]}
              for r in conn.execute(
        """SELECT pe.*, COUNT(DISTINCT f.photo_id) n FROM persons pe JOIN faces f ON f.person_id = pe.id
           JOIN photos p ON p.id = f.photo_id WHERE p.place_id = ? AND pe.merged_into IS NULL AND pe.ignored=0
           GROUP BY pe.id ORDER BY n DESC LIMIT 20""", (place_id,))]
    return {"id": p["id"], "name": p["name"], "label": place_label(p, include_country=True), "city": p["city"],
            "admin1": p["admin1"], "country": p["country"], "lat": p["lat"], "lon": p["lon"],
            "events": events, "people": people,
            "photo_count": conn.execute("SELECT COUNT(*) FROM photos WHERE place_id=? AND status='ok'",
                                        (place_id,)).fetchone()[0]}


@router.get("/map/points")
def map_points(limit: int = Query(20000, le=100000), person: int | None = None):
    """GPS points for the map, with place ids for clustering/labels."""
    conn = get_state().conn()
    sql = ("SELECT p.id, p.gps_lat lat, p.gps_lon lon, p.place_id, p.taken_ts FROM photos p "
           "WHERE p.status='ok' AND p.gps_lat IS NOT NULL")
    args: list = []
    if person:
        sql += " AND EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id AND f.person_id = ?)"
        args.append(person)
    sql += " ORDER BY p.taken_ts DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return {"points": [{"id": r["id"], "lat": round(r["lat"], 5), "lon": round(r["lon"], 5),
                        "place_id": r["place_id"], "ts": int(r["taken_ts"] or 0)} for r in rows]}
