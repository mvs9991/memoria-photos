"""GPS tracks (.gpx): shown on the map, and used to place photos that have no GPS.

A camera without GPS carried next to a phone, watch or logger that recorded a track is
the common case. A photo is placed by *time*: its wall-clock capture time is converted
to UTC (GPX times are UTC) — with the photo's own EXIF offset when it has one, else this
machine's time zone for that date — and the position is interpolated between the two
track points around it, provided both are within MAX_GAP_S. Real GPS, a Takeout
location and the user's own correction are never overridden. The result is labelled
`location_source = 'gpx'`, medium confidence, because a camera clock can be off.

Tracks are found beside the photos (any .gpx in an indexed folder) or imported with
`photointel import-gpx`. Nothing is written next to the originals.
"""
from __future__ import annotations

import bisect
import logging
import os
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .. import db
from ..metadata import ts_to_naive

log = logging.getLogger(__name__)

MAX_GAP_S = 300          # both neighbouring track points within 5 minutes of the photo
SIMPLIFY_MAX_POINTS = 1500


class GpxError(ValueError):
    pass


def parse_gpx(data: bytes) -> tuple[str | None, np.ndarray]:
    """-> (track name, array of (utc_ts, lat, lon) sorted by time). Track and route points."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise GpxError(f"not a GPX file: {exc}") from exc
    pts = []
    name = None
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "name" and name is None and el.text:
            name = el.text.strip()
        if tag not in ("trkpt", "rtept"):
            continue
        t = next((c.text for c in el if c.tag.rsplit("}", 1)[-1] == "time" and c.text), None)
        if t is None:
            continue
        try:
            ts = _parse_time(t)
            lat, lon = float(el.get("lat")), float(el.get("lon"))
        except (TypeError, ValueError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            pts.append((ts, lat, lon))
    if not pts:
        raise GpxError("no timestamped track points")
    arr = np.array(sorted(pts), dtype=np.float64)
    return name, arr


def _parse_time(text: str) -> float:
    t = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def add_track(conn: sqlite3.Connection, path: Path) -> int | None:
    path = Path(path)
    st = path.stat()
    row = conn.execute("SELECT id, mtime FROM gpx_tracks WHERE path = ?", (str(path),)).fetchone()
    if row and abs(row["mtime"] - st.st_mtime) < 1:
        return int(row["id"])
    name, arr = parse_gpx(path.read_bytes())
    vals = (str(path), name or path.stem, float(arr[0, 0]), float(arr[-1, 0]), arr.tobytes(), len(arr),
            st.st_mtime, time.time())
    if row:
        conn.execute("UPDATE gpx_tracks SET path=?, name=?, start_ts=?, end_ts=?, points=?, n_points=?, mtime=?, "
                     "added_at=? WHERE id=?", (*vals, row["id"]))
        return int(row["id"])
    return int(conn.execute("INSERT INTO gpx_tracks(path, name, start_ts, end_ts, points, n_points, mtime, added_at) "
                            "VALUES (?,?,?,?,?,?,?,?)", vals).lastrowid)


def import_file(ctx, conn: sqlite3.Connection, src: Path) -> int:
    """Copy a track into the data directory (never next to the photos) and register it."""
    dest_dir = ctx.paths.data / "gpx"
    dest_dir.mkdir(parents=True, exist_ok=True)
    parse_gpx(Path(src).read_bytes())          # refuse junk before copying it
    dest = dest_dir / Path(src).name
    if dest.resolve() != Path(src).resolve():
        shutil.copy2(src, dest)
    tid = add_track(conn, dest)
    conn.commit()
    return tid


def discover_tracks(ctx, conn: sqlite3.Connection) -> int:
    """Register .gpx files in the folders already indexed, and in <data>/gpx."""
    found = 0
    dirs = {os.path.join(r["root"], r["folder"]) for r in conn.execute(
        "SELECT DISTINCT r.path AS root, p.folder FROM photos p JOIN roots r ON r.id = p.root_id")}
    dirs.add(str(ctx.paths.data / "gpx"))
    for d in dirs:
        try:
            entries = [e for e in os.scandir(d) if e.is_file() and e.name.lower().endswith(".gpx")]
        except OSError:
            continue
        for e in entries:
            try:
                add_track(conn, Path(e.path))
                found += 1
            except (GpxError, OSError) as exc:
                log.warning("Skipping GPX %s: %s", e.path, exc)
    conn.commit()
    return found


def _utc_of(taken_ts: float, tz_offset_min: int | None) -> float:
    if tz_offset_min is not None:
        return taken_ts - tz_offset_min * 60
    local = ts_to_naive(taken_ts)                      # wall clock -> this machine's zone on that date
    return local.astimezone().timestamp()


def geotag_from_tracks(ctx, conn: sqlite3.Connection) -> dict:
    t0 = time.time()
    found = discover_tracks(ctx, conn)
    tracks = [(r["start_ts"], r["end_ts"], np.frombuffer(r["points"], dtype=np.float64).reshape(-1, 3))
              for r in conn.execute("SELECT start_ts, end_ts, points FROM gpx_tracks ORDER BY start_ts")]
    if not tracks:
        return {"tracks": 0, "placed": 0}
    rows = conn.execute(
        """SELECT id, taken_ts, tz_offset_min FROM photos
           WHERE status = 'ok' AND gps_lat IS NULL AND taken_ts IS NOT NULL
             AND date_confidence IN ('high', 'medium')
             AND COALESCE(location_source, '') NOT IN ('user', 'takeout')""").fetchall()
    updates = []
    for r in rows:
        utc = _utc_of(r["taken_ts"], r["tz_offset_min"])
        pos = _position_at(tracks, utc)
        if pos:
            updates.append((pos[0], pos[1], r["id"]))
    conn.executemany("UPDATE photos SET gps_lat = ?, gps_lon = ?, place_id = NULL, landmark_id = NULL, "
                     "location_source = 'gpx', location_confidence = 'medium' WHERE id = ?", updates)
    conn.commit()
    if updates:
        db.audit(conn, "gpx_geotag", "photo", None, {"photos": len(updates)}, actor="system")
        conn.commit()
    out = {"tracks": len(tracks), "found": found, "placed": len(updates), "seconds": round(time.time() - t0, 2)}
    log.info("GPX: %s", out)
    return out


def _position_at(tracks, utc: float) -> tuple[float, float] | None:
    for start, end, pts in tracks:
        if not (start - MAX_GAP_S <= utc <= end + MAX_GAP_S):
            continue
        times = pts[:, 0]
        i = bisect.bisect_left(times.tolist(), utc)
        before = pts[i - 1] if i > 0 else None
        after = pts[i] if i < len(pts) else None
        if before is not None and after is not None:
            if utc - before[0] > MAX_GAP_S or after[0] - utc > MAX_GAP_S:
                continue
            span = after[0] - before[0]
            f = 0.0 if span <= 0 else (utc - before[0]) / span
            return (round(float(before[1] + f * (after[1] - before[1])), 7),
                    round(float(before[2] + f * (after[2] - before[2])), 7))
        near = before if before is not None else after
        if near is not None and abs(near[0] - utc) <= MAX_GAP_S:
            return round(float(near[1]), 7), round(float(near[2]), 7)
    return None


def track_polylines(conn: sqlite3.Connection, start_ts: float | None = None, end_ts: float | None = None) -> list[dict]:
    """Tracks for the map, thinned to at most SIMPLIFY_MAX_POINTS each. Times are UTC."""
    sql = "SELECT id, name, start_ts, end_ts, points FROM gpx_tracks"
    args: list = []
    if start_ts is not None and end_ts is not None:
        sql += " WHERE end_ts >= ? AND start_ts <= ?"
        args = [start_ts, end_ts]
    out = []
    for r in conn.execute(sql + " ORDER BY start_ts", args):
        pts = np.frombuffer(r["points"], dtype=np.float64).reshape(-1, 3)
        if start_ts is not None:
            pts = pts[(pts[:, 0] >= start_ts) & (pts[:, 0] <= end_ts)]
        if len(pts) > SIMPLIFY_MAX_POINTS:
            pts = pts[np.linspace(0, len(pts) - 1, SIMPLIFY_MAX_POINTS).astype(int)]
        if len(pts):
            out.append({"id": r["id"], "name": r["name"], "start_ts": r["start_ts"], "end_ts": r["end_ts"],
                        "points": [[round(float(p[1]), 5), round(float(p[2]), 5)] for p in pts]})
    return out
