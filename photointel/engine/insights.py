"""Insights and Year in Review: what the library says about a year, or about all of it.

Everything here is counted from data already in the database — no model runs and no
number is estimated. "Furthest from home" uses the same home the trip detector uses.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

from ..geo import haversine_km
from ..metadata import naive_to_ts
from . import places as places_mod
from .people import person_label

VISIBLE = "p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0"


def compute(conn: sqlite3.Connection, year: int | None = None) -> dict:
    where, args = VISIBLE, []
    if year:
        where += " AND p.taken_ts >= ? AND p.taken_ts < ?"
        args = [naive_to_ts(datetime(year, 1, 1)), naive_to_ts(datetime(year + 1, 1, 1))]
    one = lambda sql, *a: conn.execute(sql, (*args, *a)).fetchone()[0]  # noqa: E731

    photos = one(f"SELECT COUNT(*) FROM photos p WHERE {where} AND p.media_type = 'image'")
    videos = one(f"SELECT COUNT(*) FROM photos p WHERE {where} AND p.media_type = 'video'")
    video_seconds = one(f"SELECT COALESCE(SUM(p.duration), 0) FROM photos p WHERE {where} AND p.media_type = 'video'")

    months = [0] * 12
    for r in conn.execute(f"SELECT CAST(strftime('%m', p.taken_ts, 'unixepoch') AS INT) m, COUNT(*) n "
                          f"FROM photos p WHERE {where} AND p.taken_ts IS NOT NULL GROUP BY m", args):
        months[r["m"] - 1] = r["n"]
    busiest = conn.execute(
        f"SELECT date(p.taken_ts, 'unixepoch') d, COUNT(*) n FROM photos p WHERE {where} AND p.taken_ts IS NOT NULL "
        f"GROUP BY d ORDER BY n DESC LIMIT 1", args).fetchone()

    people = [{"id": r["id"], "label": person_label(r), "cover_face_id": r["cover_face_id"], "photos": r["n"]}
              for r in conn.execute(
        f"""SELECT pe.id, pe.name, pe.display_no, pe.cover_face_id, COUNT(DISTINCT p.id) n
            FROM faces f JOIN photos p ON p.id = f.photo_id JOIN persons pe ON pe.id = f.person_id
            WHERE {where} AND pe.merged_into IS NULL AND pe.ignored = 0 AND pe.hidden = 0
            GROUP BY pe.id ORDER BY n DESC LIMIT 12""", args)]
    top_ids = [p["id"] for p in people]
    constellation = []
    if len(top_ids) > 1:
        marks = ",".join("?" * len(top_ids))
        for r in conn.execute(
                f"""SELECT a.person_id x, b.person_id y, COUNT(DISTINCT a.photo_id) n
                    FROM faces a JOIN faces b ON a.photo_id = b.photo_id AND a.person_id < b.person_id
                    JOIN photos p ON p.id = a.photo_id
                    WHERE {where} AND a.person_id IN ({marks}) AND b.person_id IN ({marks})
                    GROUP BY x, y ORDER BY n DESC LIMIT 40""", (*args, *top_ids, *top_ids)):
            constellation.append({"a": r["x"], "b": r["y"], "photos": r["n"]})
    new_people = []
    if year:
        start, end = args
        new_people = [{"id": r["id"], "label": person_label(r), "cover_face_id": r["cover_face_id"]}
                      for r in conn.execute(
            """SELECT id, name, display_no, cover_face_id FROM persons
               WHERE merged_into IS NULL AND ignored = 0 AND hidden = 0 AND photo_count >= 3
                 AND first_seen_ts >= ? AND first_seen_ts < ? ORDER BY photo_count DESC LIMIT 12""", (start, end))]

    place_rows = conn.execute(
        f"""SELECT pl.id, COALESCE(pl.city, pl.name) city, pl.country, pl.lat, pl.lon, COUNT(*) n
            FROM photos p JOIN places pl ON pl.id = p.place_id WHERE {where}
            GROUP BY COALESCE(pl.city, pl.name), pl.country ORDER BY n DESC""", args).fetchall()
    places = [{"id": r["id"], "city": r["city"], "country": r["country"], "photos": r["n"]} for r in place_rows[:12]]
    countries = sorted({r["country"] for r in place_rows if r["country"]})
    furthest = None
    home = places_mod.home_places(conn)
    if home:
        h = places_mod.place_row(conn, home[0])
        if h is not None and h["lat"] is not None:
            best = max(((haversine_km(h["lat"], h["lon"], r["lat"], r["lon"]), r) for r in place_rows
                        if r["lat"] is not None), default=None, key=lambda x: x[0])
            if best and best[0] > 1:
                furthest = {"place_id": best[1]["id"], "city": best[1]["city"], "country": best[1]["country"],
                            "km": round(best[0]), "home": h["city"] or h["name"]}

    cameras = [{"camera": " ".join(x for x in (r["camera_make"], r["camera_model"]) if x), "photos": r["n"]}
               for r in conn.execute(
        f"""SELECT p.camera_make, p.camera_model, COUNT(*) n FROM photos p
            WHERE {where} AND p.camera_model IS NOT NULL GROUP BY p.camera_make, p.camera_model
            ORDER BY n DESC LIMIT 8""", args)]
    tags = [{"name": r["name"], "photos": r["n"]} for r in conn.execute(
        f"""SELECT t.name, COUNT(*) n FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
            JOIN photos p ON p.id = pt.photo_id WHERE {where} AND pt.score >= 2.0
            GROUP BY t.id ORDER BY n DESC LIMIT 12""", args)]
    trips = [{"id": r["id"], "title": r["user_title"] or r["auto_title"], "start_ts": r["start_ts"],
              "end_ts": r["end_ts"], "photos": r["photo_count"], "cover_photo_id": r["cover_photo_id"]}
             for r in conn.execute(
        "SELECT * FROM events WHERE kind = 'trip'" + (" AND start_ts >= ? AND start_ts < ?" if year else "")
        + " ORDER BY start_ts", args)]
    events = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'event'"
                          + (" AND start_ts >= ? AND start_ts < ?" if year else ""), args).fetchone()[0]
    best = [int(r[0]) for r in conn.execute(
        f"""SELECT p.id FROM photos p WHERE {where} AND p.media_type = 'image'
              AND COALESCE(p.source_kind, '') != 'screenshot' AND p.stack_hidden = 0
            ORDER BY p.rating DESC, COALESCE(p.quality_score, 0) DESC LIMIT 60""", args)]
    best = _spread(conn, best, 12)
    years = [int(r[0]) for r in conn.execute(
        f"SELECT DISTINCT CAST(strftime('%Y', p.taken_ts, 'unixepoch') AS INT) FROM photos p "
        f"WHERE {VISIBLE} AND p.taken_ts IS NOT NULL ORDER BY 1 DESC") if r[0]]
    return {
        "year": year, "years": years,
        "totals": {"photos": photos, "videos": videos, "video_minutes": round(video_seconds / 60, 1),
                   "people": len(people), "places": len(place_rows), "countries": len(countries),
                   "trips": len(trips), "events": events},
        "months": months,
        "busiest_day": {"date": busiest["d"], "photos": busiest["n"]} if busiest else None,
        "people": people, "constellation": constellation, "new_people": new_people,
        "places": places, "countries": countries, "furthest_from_home": furthest,
        "cameras": cameras, "tags": tags, "trips": trips, "best_photo_ids": best,
    }


def _spread(conn: sqlite3.Connection, ids: list[int], n: int) -> list[int]:
    """Best photos, but at most two from any one day, so a year's highlights are a year."""
    if not ids:
        return []
    days = {int(r[0]): r[1] for r in conn.execute(
        f"SELECT id, date(taken_ts, 'unixepoch') FROM photos WHERE id IN ({','.join('?' * len(ids))})", ids)}
    per_day: Counter = Counter()
    out = []
    for i in ids:
        d = days.get(i)
        if per_day[d] >= 2:
            continue
        per_day[d] += 1
        out.append(i)
        if len(out) >= n:
            break
    return out


