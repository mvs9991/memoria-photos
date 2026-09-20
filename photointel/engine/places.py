"""Location intelligence: offline reverse geocoding plus *cautious* inference.

Confidence is always explicit:
  high    - EXIF GPS on the photo itself
  medium  - inherited from GPS photos in the same event, or taken minutes apart
  low     - folder name mentions a known place
  unknown - nothing usable
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections import Counter, defaultdict

from .. import db
from ..geo import GeoPlace, ReverseGeocoder, haversine_km

log = logging.getLogger(__name__)

GENERIC_FOLDER_TOKENS = {
    "camera", "dcim", "photos", "pictures", "images", "img", "media", "backup", "new folder", "misc",
    "whatsapp", "whatsapp images", "screenshots", "downloads", "download", "saved", "phone", "mobile",
    "old", "new", "temp", "tmp", "copy", "final", "edited", "sent", "received", "documents", "videos",
    "album", "gallery", "untitled", "100andro", "100apple", "100canon", "100nikon", "private",
    "takeout", "google photos", "archive", "export", "originals", "unsorted", "unorganised",
    "unorganized", "shareit", "telegram", "bluetooth",
}

# Words that carry no meaning alone. Google Takeout names folders "Photos from
# 2011"; stripping the generic "photos" and the year used to leave the title
# "From", which became the most common event name in a real library.
FOLDER_STOPWORDS = {
    "from", "with", "and", "the", "for", "our", "my", "me", "at", "in", "on", "of", "to",
    "by", "a", "an", "some", "other", "others", "various", "stuff", "things", "part", "set",
}


def upsert_place(conn: sqlite3.Connection, gp: GeoPlace, kind: str, city_name: str | None = None) -> int:
    row = conn.execute("SELECT id, city, admin1, country FROM places WHERE geoname_id = ?",
                       (gp.geoname_id,)).fetchone()
    if row:
        # Keep the hierarchy fresh: improvements to the resolver should show up
        # on re-geocode instead of leaving an old label in place.
        want_city = city_name or gp.name
        if (row["city"], row["admin1"], row["country"]) != (want_city, gp.admin1, gp.country):
            conn.execute("UPDATE places SET city=?, admin1=?, admin2=?, country=?, kind=? WHERE id=?",
                         (want_city, gp.admin1, gp.admin2, gp.country, kind, row["id"]))
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO places(geoname_id, name, kind, city, admin2, admin1, country_code, country, lat, lon, population) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (gp.geoname_id, gp.name, kind, city_name or gp.name, gp.admin2, gp.admin1, gp.country_code,
         gp.country, gp.lat, gp.lon, gp.population),
    )
    return int(cur.lastrowid)


def geocode_photos(ctx, conn: sqlite3.Connection, force: bool = False, progress=None) -> dict:
    if not ReverseGeocoder.available(ctx.paths.geo):
        log.warning("GeoNames data not present in %s — skipping reverse geocoding", ctx.paths.geo)
        return {"status": "no geo data"}
    rg = ReverseGeocoder.get(ctx.paths.geo)
    sql = "SELECT id, gps_lat, gps_lon FROM photos WHERE gps_lat IS NOT NULL AND status='ok'"
    if not force:
        sql += " AND place_id IS NULL"
    rows = conn.execute(sql).fetchall()
    if not rows:
        return {"geocoded": 0}
    t0 = time.time()
    cache: dict[tuple, tuple[int | None, int | None]] = {}
    updates = []
    for r in rows:
        key = (round(r["gps_lat"], 3), round(r["gps_lon"], 3))  # ~110 m cells
        hit = cache.get(key)
        if hit is None:
            res = rg.lookup(r["gps_lat"], r["gps_lon"])
            place_id = None
            if res.locality is not None and res.city is not None and res.locality.geoname_id != res.city.geoname_id:
                place_id = upsert_place(conn, res.locality, "locality", city_name=res.city.name)
            elif res.city is not None:
                place_id = upsert_place(conn, res.city, "city")
            elif res.locality is not None:
                place_id = upsert_place(conn, res.locality, "city")
            landmark_id = upsert_place(conn, res.landmark, "landmark") if res.landmark else None
            hit = (place_id, landmark_id)
            cache[key] = hit
        updates.append((hit[0], hit[1], r["id"]))
    conn.executemany(
        "UPDATE photos SET place_id=?, landmark_id=?, location_source='gps', location_confidence='high' WHERE id=?",
        updates)
    conn.commit()
    out = {"geocoded": len(updates), "distinct_places": len(cache), "seconds": round(time.time() - t0, 2)}
    log.info("Reverse geocoding: %s", out)
    return out


def infer_locations(ctx, conn: sqlite3.Connection) -> dict:
    """Give GPS-less photos a *cautious* location from context. Never overrides GPS."""
    stats = {"from_event": 0, "from_time": 0, "from_folder": 0}
    # 1) same event as geotagged photos
    rows = conn.execute(
        """SELECT p.id, p.event_id FROM photos p
           WHERE p.status='ok' AND p.gps_lat IS NULL AND p.event_id IS NOT NULL
             AND (p.location_source IS NULL OR p.location_source IN ('folder','visual'))"""
    ).fetchall()
    by_event: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        by_event[r["event_id"]].append(r["id"])
    for event_id, photo_ids in by_event.items():
        places = conn.execute(
            "SELECT place_id, COUNT(*) n FROM photos WHERE event_id=? AND place_id IS NOT NULL AND gps_lat IS NOT NULL "
            "GROUP BY place_id ORDER BY n DESC LIMIT 1", (event_id,)).fetchone()
        if not places:
            continue
        conn.executemany("UPDATE photos SET place_id=?, location_source='event', location_confidence='medium' WHERE id=?",
                         [(places["place_id"], pid) for pid in photo_ids])
        stats["from_event"] += len(photo_ids)

    # 2) photos taken within 20 minutes of a geotagged photo (same device)
    rows = conn.execute(
        """SELECT id, taken_ts, camera_model FROM photos
           WHERE status='ok' AND gps_lat IS NULL AND place_id IS NULL AND taken_ts IS NOT NULL
             AND date_confidence IN ('high','medium') ORDER BY taken_ts"""
    ).fetchall()
    if rows:
        anchors = conn.execute(
            "SELECT taken_ts, place_id, camera_model FROM photos WHERE gps_lat IS NOT NULL AND place_id IS NOT NULL "
            "AND taken_ts IS NOT NULL ORDER BY taken_ts").fetchall()
        if anchors:
            import bisect

            times = [a["taken_ts"] for a in anchors]
            updates = []
            for r in rows:
                i = bisect.bisect_left(times, r["taken_ts"])
                best = None
                for j in (i - 1, i):
                    if 0 <= j < len(anchors) and abs(anchors[j]["taken_ts"] - r["taken_ts"]) <= 1200:
                        if anchors[j]["camera_model"] == r["camera_model"]:
                            best = anchors[j]
                            break
                if best:
                    updates.append((best["place_id"], r["id"]))
            if updates:
                conn.executemany(
                    "UPDATE photos SET place_id=?, location_source='nearby_time', location_confidence='medium' WHERE id=?",
                    updates)
                stats["from_time"] = len(updates)

    # 3) folder names that name a known city (only for countries already present in the library)
    countries = [r[0] for r in conn.execute(
        "SELECT DISTINCT country_code FROM places WHERE country_code IS NOT NULL")]
    if countries and ReverseGeocoder.available(ctx.paths.geo):
        rg = ReverseGeocoder.get(ctx.paths.geo)
        folders = conn.execute(
            "SELECT DISTINCT folder FROM photos WHERE status='ok' AND place_id IS NULL AND folder != ''").fetchall()
        for f in folders:
            place = _folder_place(rg, f["folder"], set(countries))
            if place is None:
                continue
            pid = upsert_place(conn, place, "city")
            n = conn.execute(
                "UPDATE photos SET place_id=?, location_source='folder', location_confidence='low' "
                "WHERE folder=? AND place_id IS NULL AND status='ok'", (pid, f["folder"])).rowcount
            stats["from_folder"] += n
    conn.commit()
    log.info("Location inference: %s", stats)
    return stats


def _folder_place(rg: ReverseGeocoder, folder: str, countries: set[str]) -> GeoPlace | None:
    for part in reversed([p for p in folder.split("/") if p]):
        tokens = _folder_tokens(part)
        for size in (3, 2, 1):
            for i in range(len(tokens) - size + 1):
                phrase = " ".join(tokens[i:i + size])
                if phrase in GENERIC_FOLDER_TOKENS or len(phrase) < 4:
                    continue
                hits = [h for h in rg.find_by_name(phrase, min_population=50_000) if h.country_code in countries]
                if len(hits) == 1 or (hits and hits[0].population > 3 * max([h.population for h in hits[1:]] or [1])):
                    return hits[0]
    return None


def _folder_tokens(name: str) -> list[str]:
    name = re.sub(r"[_\-.]+", " ", name)
    name = re.sub(r"\d{4,}", " ", name)
    return [t for t in re.split(r"\s+", name.lower().strip()) if t and not t.isdigit()]


def home_places(conn: sqlite3.Connection, top: int = 2) -> list[int]:
    """Places where the user spends most *distinct days* — used to tell trips from everyday life."""
    rows = conn.execute(
        """SELECT place_id, COUNT(DISTINCT CAST(taken_ts / 86400 AS INT)) AS days
           FROM photos WHERE place_id IS NOT NULL AND taken_ts IS NOT NULL AND status='ok'
             AND location_confidence = 'high'
           GROUP BY place_id ORDER BY days DESC LIMIT ?""", (top * 3,)).fetchall()
    if not rows:
        return []
    out = [int(rows[0]["place_id"])]
    total_days = sum(r["days"] for r in rows)
    for r in rows[1:top]:
        if r["days"] >= 0.15 * total_days:
            out.append(int(r["place_id"]))
    return out


def place_row(conn: sqlite3.Connection, place_id: int | None):
    if not place_id:
        return None
    return conn.execute("SELECT * FROM places WHERE id=?", (place_id,)).fetchone()


def place_label(row, include_country: bool = False) -> str:
    if row is None:
        return "Unknown place"
    parts = [row["name"]]
    if row["city"] and row["city"] != row["name"]:
        parts.append(row["city"])
    if row["admin1"] and row["admin1"] not in parts:
        parts.append(row["admin1"])
    if include_country and row["country"]:
        parts.append(row["country"])
    return ", ".join([p for p in parts if p])


def distance_from_home_km(conn: sqlite3.Connection, place_id: int, home_ids: list[int]) -> float:
    if not home_ids:
        return 0.0
    p = place_row(conn, place_id)
    if p is None or p["lat"] is None:
        return 0.0
    best = 1e9
    for hid in home_ids:
        h = place_row(conn, hid)
        if h is not None and h["lat"] is not None:
            best = min(best, haversine_km(p["lat"], p["lon"], h["lat"], h["lon"]))
    return 0.0 if best > 1e8 else best
