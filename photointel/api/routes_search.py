"""Search endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from .. import db
from ..engine.people import person_label
from .deps import get_state

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(500, le=2000), llm: bool | None = None):
    state = get_state()
    conn = state.conn()
    res = state.search.search(conn, q, limit=limit, use_llm=llm)
    photos = []
    if res.photo_ids:
        marks = ",".join("?" * min(len(res.photo_ids), 2000))
        ids = res.photo_ids[:2000]
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, width, height, rotation, taken_ts FROM photos WHERE id IN ({marks})", ids)}
        for pid in ids:
            r = rows.get(pid)
            if not r:
                continue
            w, h = (r["height"], r["width"]) if r["rotation"] in (90, 270) else (r["width"], r["height"])
            photos.append({"id": pid, "ratio": round(max(0.2, min(6.0, (w or 4) / max(h or 3, 1))), 3), "rot": r["rotation"],
                           "ts": int(r["taken_ts"] or 0), "score": res.scores.get(pid)})
    return {
        "query": q, "interpretation": res.interpretation, "explanation": res.explanation,
        "result_type": res.result_type, "total": res.total, "took_ms": res.took_ms,
        "photos": photos, "events": res.events, "people": res.people, "places": res.places,
    }


@router.get("/search/suggestions")
def suggestions(q: str = Query("", max_length=80), limit: int = 8):
    """Type-ahead over people, places, events and tags."""
    state = get_state()
    conn = state.conn()
    term = q.strip().lower()
    out: list[dict] = []
    if not term:
        # Cold start: offer the most useful entry points.
        for r in conn.execute("""SELECT id, name, display_no, cover_face_id, photo_count FROM persons
                                 WHERE merged_into IS NULL AND ignored=0 AND name IS NOT NULL
                                 ORDER BY photo_count DESC LIMIT 4"""):
            out.append({"type": "person", "id": r["id"], "label": person_label(r),
                        "detail": f"{r['photo_count']:,} photos", "cover_face_id": r["cover_face_id"]})
        for r in conn.execute("""SELECT id, auto_title, user_title, photo_count, cover_photo_id FROM events
                                 WHERE kind='trip' ORDER BY start_ts DESC LIMIT 3"""):
            out.append({"type": "event", "id": r["id"], "label": r["user_title"] or r["auto_title"],
                        "detail": f"{r['photo_count']:,} photos", "cover_photo_id": r["cover_photo_id"]})
        return {"suggestions": out}
    like = db.like_contains(term)
    for r in conn.execute("""SELECT id, name, display_no, cover_face_id, photo_count FROM persons
                             WHERE merged_into IS NULL AND ignored=0 AND LOWER(COALESCE(name,'')) LIKE ? ESCAPE '\\'
                             ORDER BY photo_count DESC LIMIT ?""", (like, limit)):
        out.append({"type": "person", "id": r["id"], "label": person_label(r),
                    "detail": f"{r['photo_count']:,} photos", "cover_face_id": r["cover_face_id"]})
    for r in conn.execute("""SELECT pl.id, pl.name, pl.city, pl.country, COUNT(p.id) n FROM places pl
                             JOIN photos p ON p.place_id = pl.id
                             WHERE LOWER(pl.name) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(pl.city,'')) LIKE ? ESCAPE '\\'
                                OR LOWER(COALESCE(pl.admin1,'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(pl.country,'')) LIKE ? ESCAPE '\\'
                             GROUP BY pl.id ORDER BY n DESC LIMIT ?""", (like, like, like, like, limit)):
        out.append({"type": "place", "id": r["id"], "label": r["name"],
                    "detail": f"{r['n']:,} photos · {r['country'] or ''}".strip(" ·")})
    for r in conn.execute("""SELECT id, kind, auto_title, user_title, photo_count, cover_photo_id FROM events
                             WHERE LOWER(COALESCE(user_title, auto_title)) LIKE ? ESCAPE '\\'
                             ORDER BY start_ts DESC LIMIT ?""", (like, limit)):
        out.append({"type": "event", "id": r["id"], "label": r["user_title"] or r["auto_title"],
                    "detail": f"{r['photo_count']:,} photos", "cover_photo_id": r["cover_photo_id"]})
    for r in conn.execute("""SELECT t.name, COUNT(*) n FROM tags t JOIN photo_tags pt ON pt.tag_id = t.id
                             WHERE t.name LIKE ? ESCAPE '\\' AND pt.score >= 2.0 GROUP BY t.id ORDER BY n DESC LIMIT ?""",
                          (like, limit)):
        out.append({"type": "tag", "label": r["name"], "detail": f"{r['n']:,} photos"})
    return {"suggestions": out[: limit * 2]}
