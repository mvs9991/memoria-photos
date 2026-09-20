"""Event and trip detection.

Signals combined (no single one is trusted alone):
  * capture time gaps, with an adaptive threshold
  * GPS jumps (a 30 km move inside 45 minutes ends an event)
  * calendar-day / overnight merging at the same place
  * folder names (a weak hint for titles and for grouping, never the sole basis)
  * semantic tags (event category) and people (who was there)

Events are re-derived from scratch on every run, then matched back onto existing
rows by photo overlap so ids and user-edited titles survive.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .. import db
from ..geo import haversine_km
from ..metadata import ts_to_naive
from . import places as places_mod

log = logging.getLogger(__name__)

MONTHS_LOWER = {"january", "february", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
                "sept", "oct", "nov", "dec"}

CATEGORY_TITLES = {
    "wedding": "Wedding", "birthday": "Birthday", "party": "Party", "festival": "Festival",
    "religious ceremony": "Ceremony", "mehendi": "Mehendi", "graduation": "Graduation",
    "concert": "Concert", "sports event": "Match", "family gathering": "Family Gathering",
    "dinner": "Dinner", "picnic": "Picnic", "conference": "Conference", "school event": "School Event",
    "baby shower": "Baby Shower", "road trip": "Road Trip", "beach": "Beach Day", "mountains": "Mountains",
    "snow": "Snow Day", "temple": "Temple Visit", "monument": "Sightseeing", "waterfall": "Waterfall",
    "lake": "Lake Day", "museum": "Museum Visit", "zoo": "Zoo Visit", "amusement park": "Amusement Park",
    "swimming pool": "Pool Day", "hiking": "Hike", "camping": "Camping",
}


@dataclass
class EventParams:
    gap_hours: float = 2.5
    travel_gap_minutes: float = 45.0
    travel_km: float = 30.0
    min_gap_minutes: float = 20.0
    overnight_merge_hours: float = 6.0
    multiday_merge_hours: float = 20.0
    min_photos: int = 4
    max_event_photos: int = 1200     # beyond this, split at the next day boundary
    trip_min_km: float = 60.0
    trip_max_gap_days: int = 2
    category_min_share: float = 0.18


@dataclass
class Segment:
    photo_ids: list[int] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0
    place_ids: Counter = field(default_factory=Counter)
    folders: Counter = field(default_factory=Counter)
    coords: list[tuple[float, float]] = field(default_factory=list)


def detect_events(ctx, conn: sqlite3.Connection, params: EventParams | None = None, progress=None) -> dict:
    p = params or EventParams()
    t0 = time.time()
    rows = conn.execute(
        """SELECT id, taken_ts, date_confidence, gps_lat, gps_lon, place_id, folder, source_kind
           FROM photos WHERE status='ok' AND taken_ts IS NOT NULL
             AND COALESCE(source_kind,'unknown') != 'screenshot'
           ORDER BY taken_ts"""
    ).fetchall()
    if not rows:
        return {"events": 0}

    city_of = _city_map(conn)
    segments = _segment(rows, p)
    segments = _merge_same_day(segments, p, city_of)
    segments = _merge_multiday(conn, segments, p, city_of)
    keep = [s for s in segments if len(s.photo_ids) >= p.min_photos]
    log.info("Event segmentation: %d segments -> %d events (min %d photos)", len(segments), len(keep), p.min_photos)

    built = [_build_event(conn, s) for s in keep]
    ids = _persist_events(conn, built)
    trips = _detect_trips(conn, p)
    _update_event_stats(conn)
    conn.commit()
    db.bump_generation(conn, "events")
    conn.commit()
    out = {"events": len(ids), "trips": trips, "unassigned_photos":
           conn.execute("SELECT COUNT(*) FROM photos WHERE status='ok' AND event_id IS NULL").fetchone()[0],
           "seconds": round(time.time() - t0, 2)}
    log.info("Event detection: %s", out)
    return out


def _segment(rows, p: EventParams) -> list[Segment]:
    segs: list[Segment] = []
    cur = Segment()
    prev = None
    for r in rows:
        if prev is not None:
            gap = (r["taken_ts"] - prev["taken_ts"]) / 3600.0
            split = gap > p.gap_hours
            if not split and gap * 60 >= p.travel_gap_minutes:
                if all(x is not None for x in (r["gps_lat"], prev["gps_lat"])):
                    d = haversine_km(prev["gps_lat"], prev["gps_lon"], r["gps_lat"], r["gps_lon"])
                    split = d >= p.travel_km
            # A very dense stream (timelapse, burst-heavy phone) must not become one
            # unbrowsable event: force a break at the next day boundary.
            if (not split and len(cur.photo_ids) >= p.max_event_photos
                    and ts_to_naive(prev["taken_ts"]).date() != ts_to_naive(r["taken_ts"]).date()):
                split = True
            if split and gap * 60 >= p.min_gap_minutes and cur.photo_ids:
                segs.append(cur)
                cur = Segment()
            elif split and cur.photo_ids and len(cur.photo_ids) >= p.max_event_photos:
                segs.append(cur)
                cur = Segment()
        cur.photo_ids.append(r["id"])
        cur.start = cur.start or r["taken_ts"]
        cur.end = r["taken_ts"]
        if r["place_id"]:
            cur.place_ids[r["place_id"]] += 1
        if r["folder"]:
            cur.folders[r["folder"]] += 1
        if r["gps_lat"] is not None:
            cur.coords.append((r["gps_lat"], r["gps_lon"]))
        prev = r
    if cur.photo_ids:
        segs.append(cur)
    return segs


def _same_place(a: Segment, b: Segment, city_of: dict[int, str] | None = None) -> bool:
    """Same *city*, not same GPS point.

    Photos taken across one city resolve to different neighbourhood records
    (Charminar and Golconda are both Hyderabad), so comparing raw place ids
    would split a single day out in town into several events.
    """
    if a.place_ids and b.place_ids:
        pa = a.place_ids.most_common(1)[0][0]
        pb = b.place_ids.most_common(1)[0][0]
        if pa == pb:
            return True
        if city_of:
            ca, cb = city_of.get(pa), city_of.get(pb)
            if ca and cb and ca == cb:
                return True
    if a.coords and b.coords:
        return haversine_km(*a.coords[-1], *b.coords[0]) < 15
    if not a.place_ids or not b.place_ids:
        return True  # no location info: fall back to the time rule alone
    return False


def _city_map(conn) -> dict[int, str]:
    return {int(r[0]): (r[1] or r[2] or "") for r in
            conn.execute("SELECT id, city, name FROM places")}


def _merge_same_day(segs: list[Segment], p: EventParams, city_of: dict[int, str] | None = None) -> list[Segment]:
    """Two sessions on the same day at the same place are one event (lunch, then evening)."""
    out: list[Segment] = []
    for s in segs:
        if out:
            prev = out[-1]
            gap_h = (s.start - prev.end) / 3600.0
            same_day = ts_to_naive(prev.end).date() == ts_to_naive(s.start).date()
            overnight = gap_h <= p.overnight_merge_hours and not same_day
            if ((same_day or overnight) and gap_h <= 10 and _same_place(prev, s, city_of)
                    and len(prev.photo_ids) + len(s.photo_ids) <= p.max_event_photos * 2):
                _absorb(prev, s)
                continue
        out.append(s)
    return out


def _merge_multiday(conn, segs: list[Segment], p: EventParams, city_of: dict[int, str] | None = None) -> list[Segment]:
    """Consecutive days at the same place with the same dominant folder = one multi-day event."""
    out: list[Segment] = []
    for s in segs:
        if out:
            prev = out[-1]
            gap_h = (s.start - prev.end) / 3600.0
            if (gap_h <= p.multiday_merge_hours and _same_place(prev, s, city_of)
                    and len(prev.photo_ids) + len(s.photo_ids) <= p.max_event_photos * 2):
                pf = prev.folders.most_common(1)[0][0] if prev.folders else None
                sf = s.folders.most_common(1)[0][0] if s.folders else None
                if pf and pf == sf:
                    _absorb(prev, s)
                    continue
        out.append(s)
    return out


def _absorb(target: Segment, other: Segment) -> None:
    target.photo_ids.extend(other.photo_ids)
    target.end = other.end
    target.place_ids.update(other.place_ids)
    target.folders.update(other.folders)
    target.coords.extend(other.coords)


def _build_event(conn: sqlite3.Connection, seg: Segment) -> dict:
    photo_ids = seg.photo_ids
    place_id = seg.place_ids.most_common(1)[0][0] if seg.place_ids else None
    lat = lon = None
    if seg.coords:
        lat = float(np.median([c[0] for c in seg.coords]))
        lon = float(np.median([c[1] for c in seg.coords]))
    category, cat_conf = _event_category(conn, photo_ids)
    folder_hint = _folder_hint(seg)
    place = places_mod.place_row(conn, place_id)
    title = _make_title(category, place, folder_hint, seg)
    loc_conf = _location_confidence(conn, photo_ids)
    cover = _pick_cover(conn, photo_ids)
    return {
        "photo_ids": photo_ids, "start": seg.start, "end": seg.end, "place_id": place_id,
        "lat": lat, "lon": lon, "category": category, "category_confidence": cat_conf,
        "auto_title": title, "folder_hint": folder_hint, "cover_photo_id": cover,
        "location_confidence": loc_conf,
    }


def _event_category(conn: sqlite3.Connection, photo_ids: list[int], min_density: float = 0.25,
                    min_share: float = 0.08, dominance: float = 1.6):
    """Pick the event category from the tags of its photos.

    Scored by *evidence density* — the total tag strength spread over every photo
    in the event — rather than a plain share, because group shots and portraits
    dilute scene tags badly (a beach day may only tag 15% of its frames "beach").
    A category is only used when it clearly beats the runner-up, since a wrong
    title ("Snow Day") is worse for the user than a neutral one ("Hyderabad").
    """
    from .tags import TAG_FILTER_Z

    if not photo_ids:
        return None, None
    sample = photo_ids if len(photo_ids) <= 900 else list(
        np.random.default_rng(7).choice(photo_ids, 900, replace=False))
    q = ",".join("?" * len(sample))
    rows = conn.execute(
        f"""SELECT t.name, t.category, COUNT(*) AS n, SUM(pt.score) AS total
            FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
            WHERE pt.photo_id IN ({q}) AND pt.score >= ? AND t.name IN ({','.join('?' * len(CATEGORY_TITLES))})
            GROUP BY t.id""", (*sample, TAG_FILTER_Z, *CATEGORY_TITLES.keys())).fetchall()
    if not rows:
        return None, None
    scored = []
    for r in rows:
        share = r["n"] / len(sample)
        density = float(r["total"]) / len(sample)
        if r["category"] == "event":
            density *= 1.25  # an explicit occasion beats incidental scenery
        scored.append((density, share, r["name"]))
    scored.sort(reverse=True)
    best_density, best_share, best_name = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if best_density < min_density or best_share < min_share:
        return None, None
    if runner > 0 and best_density / runner < dominance:
        return None, None
    return best_name, round(min(1.0, best_density / 1.5), 3)


def _folder_hint(seg: Segment) -> str | None:
    if not seg.folders:
        return None
    folder, count = seg.folders.most_common(1)[0]
    if count < 0.7 * len(seg.photo_ids):
        return None
    leaf = folder.rstrip("/").split("/")[-1] if folder else ""
    tokens = places_mod._folder_tokens(leaf)
    month_tokens = {m for m in MONTHS_LOWER}
    tokens = [t for t in tokens if t not in month_tokens]
    meaningful = [t for t in tokens if t not in places_mod.GENERIC_FOLDER_TOKENS and len(t) > 2]
    # A hint made only of connectives ("from", "and", "with") names nothing. A plain
    # title beats a wrong one, so fall through to the place or date instead.
    if not any(t not in places_mod.FOLDER_STOPWORDS for t in meaningful):
        return None
    meaningful = [t for t in meaningful if t not in places_mod.FOLDER_STOPWORDS]
    if not meaningful:
        return None
    return " ".join(w.upper() if len(w) <= 3 and w.isalpha() else w.capitalize() for w in meaningful).strip() or None


def _make_title(category: str | None, place, folder_hint: str | None, seg: Segment) -> str:
    city = None
    if place is not None:
        city = place["city"] or place["name"]
    cat_title = CATEGORY_TITLES.get(category) if category else None
    if cat_title and city:
        return f"{cat_title} in {city}"
    if cat_title:
        return cat_title
    if folder_hint:
        return folder_hint if not city else f"{folder_hint}"
    if city:
        return city
    d = ts_to_naive(seg.start)
    return d.strftime("%B %Y")


def _location_confidence(conn: sqlite3.Connection, photo_ids: list[int]) -> str:
    q = ",".join("?" * min(len(photo_ids), 900))
    sample = photo_ids[:900]
    rows = conn.execute(
        f"SELECT location_confidence, COUNT(*) n FROM photos WHERE id IN ({q}) GROUP BY location_confidence",
        sample).fetchall()
    best = "unknown"
    order = {"high": 3, "medium": 2, "low": 1, "unknown": 0, None: 0}
    for r in rows:
        if order.get(r["location_confidence"], 0) > order.get(best, 0):
            best = r["location_confidence"] or "unknown"
    return best


def _pick_cover(conn: sqlite3.Connection, photo_ids: list[int]) -> int | None:
    photo_ids = photo_ids[:900]  # SQLite bound-parameter limit; a sample is enough to pick a cover
    q = ",".join("?" * len(photo_ids))
    row = conn.execute(
        f"""SELECT id FROM photos WHERE id IN ({q}) AND status='ok'
            ORDER BY (COALESCE(quality_score, 40) + CASE WHEN face_count > 0 THEN 12 ELSE 0 END
                      + CASE WHEN COALESCE(source_kind,'') IN ('camera','phone') THEN 8 ELSE 0 END) DESC
            LIMIT 1""", photo_ids).fetchone()
    return int(row[0]) if row else None


def _persist_events(conn: sqlite3.Connection, built: list[dict]) -> list[int]:
    """Match new events to existing ones by photo overlap so ids/user titles survive."""
    now = time.time()
    old_events = conn.execute("SELECT id, user_title, kind FROM events WHERE kind='event'").fetchall()
    old_photos: dict[int, set[int]] = {}
    for e in old_events:
        old_photos[e["id"]] = {int(r[0]) for r in conn.execute("SELECT id FROM photos WHERE event_id=?", (e["id"],))}
    old_titles = {e["id"]: e["user_title"] for e in old_events}

    used_old: set[int] = set()
    result_ids = []
    conn.execute("UPDATE photos SET event_id = NULL WHERE event_id IS NOT NULL")
    for ev in built:
        new_set = set(ev["photo_ids"])
        best_id, best_j = None, 0.0
        for oid, oset in old_photos.items():
            if oid in used_old or not oset:
                continue
            inter = len(new_set & oset)
            if not inter:
                continue
            j = inter / len(new_set | oset)
            if j > best_j:
                best_id, best_j = oid, j
        signature = f"{int(ev['start'])}-{int(ev['end'])}-{len(new_set)}"
        if best_id is not None and best_j >= 0.5:
            used_old.add(best_id)
            eid = best_id
            conn.execute(
                """UPDATE events SET start_ts=?, end_ts=?, photo_count=?, place_id=?, lat=?, lon=?, category=?,
                   category_confidence=?, auto_title=?, folder_hint=?, cover_photo_id=?, location_confidence=?,
                   signature=?, updated_at=? WHERE id=?""",
                (ev["start"], ev["end"], len(new_set), ev["place_id"], ev["lat"], ev["lon"], ev["category"],
                 ev["category_confidence"], ev["auto_title"], ev["folder_hint"], ev["cover_photo_id"],
                 ev["location_confidence"], signature, now, eid))
        else:
            cur = conn.execute(
                """INSERT INTO events(kind, auto_title, category, category_confidence, start_ts, end_ts,
                   photo_count, place_id, lat, lon, cover_photo_id, folder_hint, location_confidence,
                   signature, created_at, updated_at)
                   VALUES ('event',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ev["auto_title"], ev["category"], ev["category_confidence"], ev["start"], ev["end"],
                 len(new_set), ev["place_id"], ev["lat"], ev["lon"], ev["cover_photo_id"], ev["folder_hint"],
                 ev["location_confidence"], signature, now, now))
            eid = int(cur.lastrowid)
        result_ids.append(eid)
        conn.executemany("UPDATE photos SET event_id=? WHERE id=?", [(eid, pid) for pid in ev["photo_ids"]])
    # Remove events that no longer exist (keeps user-renamed ones only if they still have photos).
    stale = [e["id"] for e in old_events if e["id"] not in result_ids]
    if stale:
        conn.executemany("DELETE FROM events WHERE id=? AND kind='event'", [(s,) for s in stale])
    return result_ids


def _detect_trips(conn: sqlite3.Connection, p: EventParams) -> int:
    """A trip = consecutive days of events far from home."""
    conn.execute("DELETE FROM trip_photos")
    conn.execute("DELETE FROM events WHERE kind='trip'")
    conn.execute("UPDATE events SET parent_id=NULL WHERE kind='event'")
    home = places_mod.home_places(conn)
    events = conn.execute(
        "SELECT id, start_ts, end_ts, place_id, photo_count, category, lat, lon FROM events WHERE kind='event' "
        "ORDER BY start_ts").fetchall()
    if not events:
        return 0
    away = []
    for e in events:
        far = False
        if e["place_id"] and home:
            far = places_mod.distance_from_home_km(conn, e["place_id"], home) >= p.trip_min_km
        elif e["place_id"] and not home:
            far = False
        away.append(far)

    trips = 0
    i = 0
    now = time.time()
    while i < len(events):
        if not away[i]:
            i += 1
            continue
        j = i
        while (j + 1 < len(events) and away[j + 1]
               and (events[j + 1]["start_ts"] - events[j]["end_ts"]) <= p.trip_max_gap_days * 86400):
            j += 1
        group = events[i:j + 1]
        days = (group[-1]["end_ts"] - group[0]["start_ts"]) / 86400.0
        total_photos = sum(g["photo_count"] for g in group)
        # A single short outing isn't a trip; require an overnight stay or several events.
        if days >= 0.75 or len(group) >= 2:
            place_counter = Counter(g["place_id"] for g in group if g["place_id"])
            place_id = place_counter.most_common(1)[0][0] if place_counter else None
            place = places_mod.place_row(conn, place_id)
            city = (place["city"] or place["name"]) if place is not None else "Trip"
            title = f"{city} Trip"
            cover = max(group, key=lambda g: g["photo_count"])["cover_photo_id"] if False else None
            cover_row = conn.execute(
                "SELECT cover_photo_id FROM events WHERE id=?", (max(group, key=lambda g: g['photo_count'])["id"],)
            ).fetchone()
            cover = cover_row[0] if cover_row else None
            places_json = json.dumps([pid for pid, _ in place_counter.most_common(6)])
            cur = conn.execute(
                """INSERT INTO events(kind, auto_title, start_ts, end_ts, photo_count, place_id, places_json,
                   cover_photo_id, lat, lon, category, location_confidence, signature, created_at, updated_at)
                   VALUES ('trip',?,?,?,?,?,?,?,?,?,'trip',?,?,?,?)""",
                (title, group[0]["start_ts"], group[-1]["end_ts"], total_photos, place_id, places_json, cover,
                 group[0]["lat"], group[0]["lon"], "medium", f"trip-{int(group[0]['start_ts'])}", now, now))
            tid = int(cur.lastrowid)
            conn.executemany("UPDATE events SET parent_id=? WHERE id=?", [(tid, g["id"]) for g in group])
            conn.execute(
                f"INSERT OR IGNORE INTO trip_photos(trip_id, photo_id) SELECT ?, id FROM photos WHERE event_id IN "
                f"({','.join('?' * len(group))})", (tid, *[g["id"] for g in group]))
            trips += 1
        i = j + 1
    return trips


def _update_event_stats(conn: sqlite3.Connection) -> None:
    conn.execute(
        """UPDATE events SET
             photo_count = COALESCE((SELECT COUNT(*) FROM photos p WHERE p.event_id = events.id), photo_count),
             people_count = COALESCE((SELECT COUNT(DISTINCT f.person_id) FROM faces f
                                      JOIN photos p ON p.id = f.photo_id
                                      WHERE p.event_id = events.id AND f.person_id IS NOT NULL), 0)
           WHERE kind='event'""")
    conn.execute(
        """UPDATE events SET
             people_count = COALESCE((SELECT COUNT(DISTINCT f.person_id) FROM faces f
                                      JOIN trip_photos tp ON tp.photo_id = f.photo_id
                                      WHERE tp.trip_id = events.id AND f.person_id IS NOT NULL), 0)
           WHERE kind='trip'""")
    for row in conn.execute("SELECT id, kind FROM events").fetchall():
        conn.execute("UPDATE events SET summary=?, summary_source='template' WHERE id=?",
                     (build_summary(conn, row["id"]), row["id"]))


def build_summary(conn: sqlite3.Connection, event_id: int) -> str:
    from .people import person_label
    from .tags import top_tags_for_photos

    ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if ev is None:
        return ""
    if ev["kind"] == "trip":
        photo_ids = [int(r[0]) for r in conn.execute("SELECT photo_id FROM trip_photos WHERE trip_id=?", (event_id,))]
    else:
        photo_ids = [int(r[0]) for r in conn.execute("SELECT id FROM photos WHERE event_id=?", (event_id,))]
    if not photo_ids:
        return ""
    start, end = ts_to_naive(ev["start_ts"]), ts_to_naive(ev["end_ts"])
    days = (end.date() - start.date()).days + 1
    bits = [f"{len(photo_ids):,} photos"]
    if days > 1:
        bits.append(f"{days} days")
    place = places_mod.place_row(conn, ev["place_id"])
    if place is not None:
        bits.append(places_mod.place_label(place))
    people = conn.execute(
        f"""SELECT p.id, p.name, p.display_no, COUNT(DISTINCT f.photo_id) n FROM faces f
            JOIN persons p ON p.id = f.person_id
            WHERE f.photo_id IN ({','.join('?' * min(len(photo_ids), 900))}) AND p.ignored = 0 AND p.merged_into IS NULL
            GROUP BY p.id ORDER BY n DESC LIMIT 4""", photo_ids[:900]).fetchall()
    sentence = " · ".join(bits)
    if people:
        names = [person_label(p) for p in people[:3]]
        more = max(0, len(people) - 3)
        if more:
            names.append(f"{more} other" if more == 1 else f"{more} others")
        sentence += "\nWith " + _join_names(names)
    tags = [t for t, _ in top_tags_for_photos(conn, photo_ids[:300], limit=4)]
    if tags:
        sentence += "\nMostly " + _join_names(tags)
    return sentence


def _join_names(items: list[str]) -> str:
    """a, b and c — without the double 'and' that list slicing produces."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def event_title(row) -> str:
    return row["user_title"] or row["auto_title"]


def date_range_label(start_ts: float, end_ts: float) -> str:
    """Human date range. Built manually: strftime day-without-padding differs across platforms."""
    s, e = ts_to_naive(start_ts), ts_to_naive(end_ts)
    if s.date() == e.date():
        return f"{s.day} {s.strftime('%B %Y')}"
    if (s.year, s.month) == (e.year, e.month):
        return f"{s.day}–{e.day} {s.strftime('%B %Y')}"
    if s.year == e.year:
        return f"{s.day} {s.strftime('%b')} – {e.day} {e.strftime('%b %Y')}"
    return f"{s.strftime('%b %Y')} – {e.strftime('%b %Y')}"
