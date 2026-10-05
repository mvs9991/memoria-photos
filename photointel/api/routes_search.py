"""Search endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import db
from ..engine.people import person_label
from .cache import value_until_db_changes
from .deps import get_state

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(500, ge=1, le=2000), llm: bool | None = None):
    q = q[:500]           # longer is not a search; the parser's name matching grows with every word
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
    return JSONResponse({
        "query": q, "interpretation": res.interpretation, "explanation": res.explanation,
        "result_type": res.result_type, "total": res.total, "took_ms": res.took_ms,
        "photos": photos, "events": res.events, "people": res.people, "places": res.places,
    })


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
                                 WHERE merged_into IS NULL AND ignored=0 AND name IS NOT NULL AND photo_count > 0
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
                             WHERE merged_into IS NULL AND ignored=0 AND photo_count > 0 AND LOWER(COALESCE(name,'')) LIKE ? ESCAPE '\\'
                             ORDER BY photo_count DESC LIMIT ?""", (like, limit)):
        out.append({"type": "person", "id": r["id"], "label": person_label(r),
                    "detail": f"{r['photo_count']:,} photos", "cover_face_id": r["cover_face_id"]})
    # Places and tags with their photo counts are counted once and kept until the database changes: counting
    # them for every key typed in the search box was ~95 ms a keystroke on a 31k-photo library.
    places, tags = value_until_db_changes("suggest-places", lambda: _place_counts(conn)),         value_until_db_changes("suggest-tags", lambda: _tag_counts(conn))
    for pid, name, city, admin1, country, n in [p for p in places if any(term in (x or "").lower() for x in p[1:5])][:limit]:
        out.append({"type": "place", "id": pid, "label": name,
                    "detail": f"{n:,} photos · {country or ''}".strip(" ·")})
    for r in conn.execute("""SELECT id, kind, auto_title, user_title, photo_count, cover_photo_id FROM events
                             WHERE LOWER(COALESCE(user_title, auto_title)) LIKE ? ESCAPE '\\'
                             ORDER BY start_ts DESC LIMIT ?""", (like, limit)):
        out.append({"type": "event", "id": r["id"], "label": r["user_title"] or r["auto_title"],
                    "detail": f"{r['photo_count']:,} photos", "cover_photo_id": r["cover_photo_id"]})
    for name, n in [t for t in tags if term in t[0].lower()][:limit]:
        out.append({"type": "tag", "label": name, "detail": f"{n:,} photos"})
    return {"suggestions": out[: limit * 2]}


def _place_counts(conn) -> list[tuple]:
    """(id, name, city, admin1, country, visible photos) for every place with a photo, most photos first."""
    return [tuple(r) for r in conn.execute(
        """SELECT pl.id, pl.name, pl.city, pl.admin1, pl.country, COUNT(p.id) n FROM places pl
           JOIN photos p ON p.place_id = pl.id AND p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0
           GROUP BY pl.id ORDER BY n DESC, pl.id""")]


def _tag_counts(conn) -> list[tuple]:
    """(name, visible photos it clearly applies to) for every tag, most photos first."""
    return [tuple(r) for r in conn.execute(
        """SELECT t.name, COUNT(*) n FROM tags t JOIN photo_tags pt ON pt.tag_id = t.id
           JOIN photos p ON p.id = pt.photo_id AND p.status = 'ok' AND p.hidden = 0
           WHERE pt.score >= 2.0 GROUP BY t.id ORDER BY n DESC, t.id""")]

