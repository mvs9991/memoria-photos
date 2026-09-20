"""Post-analysis stages: geocoding, tagging, people, events, duplicates, search index.

These run after photo analysis (and can be re-run alone with `index --post-only`).
Each stage is independent and idempotent.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable

from .. import db
from ..engine import duplicates as dup_mod
from ..engine import events as events_mod
from ..engine import people as people_mod
from ..engine import places as places_mod
from ..engine import tags as tags_mod

log = logging.getLogger(__name__)

STAGES = ["geocode", "tags", "quality", "people", "events", "locations", "duplicates", "search-index"]


def run_post_stages(ctx, conn: sqlite3.Connection, stages: list[str] | None = None,
                    progress: Callable[[str, int, int, str], None] | None = None,
                    full_recluster: bool = False) -> dict:
    stages = stages or STAGES
    out: dict = {}
    total = len(stages)

    def report(i: int, name: str, msg: str = "") -> None:
        if progress:
            progress(name, i, total, msg)

    for i, stage in enumerate(stages):
        t0 = time.time()
        report(i, stage, f"Running {stage}")
        try:
            if stage == "geocode":
                out[stage] = places_mod.geocode_photos(ctx, conn)
            elif stage == "tags":
                if ctx.settings.semantic_model:
                    out[stage] = tags_mod.tag_photos(ctx, conn)
            elif stage == "quality":
                out[stage] = tags_mod.recompute_quality(conn, only_missing=False)
            elif stage == "people":
                out[stage] = people_mod.recluster(ctx, conn, full=full_recluster)
            elif stage == "events":
                out[stage] = events_mod.detect_events(ctx, conn)
            elif stage == "locations":
                out[stage] = places_mod.infer_locations(ctx, conn)
                # Re-title events now that more photos have a location.
                events_mod.detect_events(ctx, conn)
            elif stage == "duplicates":
                out[stage] = dup_mod.find_duplicates(ctx, conn)
            elif stage == "search-index":
                out[stage] = rebuild_fts(conn)
        except Exception as exc:
            log.exception("Post stage %s failed", stage)
            out[stage] = {"error": str(exc)}
        log.info("Stage %s finished in %.1fs", stage, time.time() - t0)
    report(total, "done", "Post-processing complete")
    return out


def rebuild_fts(conn: sqlite3.Connection, batch: int = 5000) -> dict:
    """Rebuild the keyword index (filenames, folders, tags, places, people, events, captions)."""
    t0 = time.time()
    conn.execute("DELETE FROM photo_fts")
    rows = conn.execute(
        """SELECT p.id, p.filename, p.folder, p.caption, p.camera_make, p.camera_model, p.source_kind,
                  pl.name AS place_name, pl.city, pl.admin1, pl.country,
                  lm.name AS landmark,
                  e.auto_title, e.user_title
           FROM photos p
           LEFT JOIN places pl ON pl.id = p.place_id
           LEFT JOIN places lm ON lm.id = p.landmark_id
           LEFT JOIN events e ON e.id = p.event_id
           WHERE p.status = 'ok'"""
    ).fetchall()
    tag_map: dict[int, list[str]] = {}
    for pid, name in conn.execute(
            "SELECT pt.photo_id, t.name FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.score >= 1.5"):
        tag_map.setdefault(int(pid), []).append(name)
    people_map: dict[int, list[str]] = {}
    for pid, name in conn.execute(
            "SELECT f.photo_id, p.name FROM faces f JOIN persons p ON p.id = f.person_id WHERE p.name IS NOT NULL"):
        people_map.setdefault(int(pid), []).append(name)

    payload = []
    for r in rows:
        parts = [
            r["filename"].rsplit(".", 1)[0].replace("_", " ").replace("-", " "),
            r["folder"].replace("/", " ").replace("_", " ").replace("-", " "),
            r["caption"] or "", r["place_name"] or "", r["city"] or "", r["admin1"] or "", r["country"] or "",
            r["landmark"] or "", r["user_title"] or r["auto_title"] or "",
            r["camera_make"] or "", r["camera_model"] or "", r["source_kind"] or "",
            " ".join(tag_map.get(int(r["id"]), [])), " ".join(people_map.get(int(r["id"]), [])),
        ]
        payload.append((int(r["id"]), " ".join(p for p in parts if p)))
    for i in range(0, len(payload), batch):
        conn.executemany("INSERT INTO photo_fts(rowid, text) VALUES (?,?)", payload[i:i + batch])
    conn.commit()
    db.bump_generation(conn, "fts")
    conn.commit()
    return {"indexed": len(payload), "seconds": round(time.time() - t0, 2)}
