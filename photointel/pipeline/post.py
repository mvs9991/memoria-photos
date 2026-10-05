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
from ..engine import gpx as gpx_mod
from ..engine import live as live_mod
from ..engine import ocr as ocr_mod
from ..engine import takeout as takeout_mod
from ..engine import people as people_mod
from ..engine import places as places_mod
from ..engine import stacks as stacks_mod
from ..engine import tags as tags_mod

log = logging.getLogger(__name__)

# Order matters: live pairing decides which files are clustered and listed at all;
# Takeout locations must exist before geocoding; OCR picks its candidates by tag.
STAGES = ["live-photos", "uploads", "takeout", "icloud", "xmp", "gpx", "geocode", "tags", "ocr", "quality", "people", "events", "locations",
          "duplicates", "stacks", "colors", "search-index"]


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
            if stage == "live-photos":
                out[stage] = live_mod.pair_live_photos(conn)
                out[stage]["motion"] = live_mod.backfill_motion_offsets(conn)
            elif stage == "takeout":
                if ctx.settings.takeout_import:
                    out[stage] = takeout_mod.import_takeout(ctx, conn)
            elif stage == "colors":
                from ..engine.colors import compute_colors

                out[stage] = compute_colors(ctx, conn)
            elif stage == "uploads":
                from ..engine.uploads import link_to_albums

                out[stage] = link_to_albums(conn)
            elif stage == "xmp":
                if ctx.settings.takeout_import:        # the same switch: other apps' metadata
                    from ..engine.xmp_import import import_xmp

                    out[stage] = import_xmp(ctx, conn)
            elif stage == "icloud":
                if ctx.settings.takeout_import:        # one switch for reading other services' exports
                    from ..engine.icloud import import_icloud

                    out[stage] = import_icloud(ctx, conn)
            elif stage == "gpx":
                out[stage] = gpx_mod.geotag_from_tracks(ctx, conn)
            elif stage == "ocr":
                if ctx.settings.ocr_enabled:
                    out[stage] = ocr_mod.ocr_photos(ctx, conn)
            elif stage == "geocode":
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
                # Re-title events now that more photos have a location; when none gained one (most runs on the
                # real library), the events stage just before already left them right.
                if any(out[stage].values()):
                    events_mod.detect_events(ctx, conn)
            elif stage == "duplicates":
                out[stage] = dup_mod.find_duplicates(ctx, conn)
            elif stage == "stacks":
                out[stage] = stacks_mod.build_stacks(conn, enabled=ctx.settings.stacks_enabled)
            elif stage == "search-index":
                out[stage] = rebuild_fts(conn)
        except Exception as exc:
            log.exception("Post stage %s failed", stage)
            out[stage] = {"error": str(exc)}
            try:                       # a failed stage's half-done writes must not ride along into the next
                conn.rollback()
            except sqlite3.Error:
                pass
        else:
            conn.commit()
        # Every stage ends with this connection idle. The progress report that opens the next stage
        # goes through a *different* connection; if this one still held an uncommitted write, that
        # report waited out the full 60 s busy timeout for a lock held by the very thread waiting on
        # it. Four stages that did almost no work (0.1-0.2 s on their own) each took 100-260 s inside
        # a job, and the database was write-locked for a fifth of every hour a server was running.
        log.info("Stage %s finished in %.1fs", stage, time.time() - t0)
    report(total, "done", "Post-processing complete")
    return out


def rebuild_fts(conn: sqlite3.Connection, batch: int = 5000) -> dict:
    """Rebuild the keyword index (filenames, folders, tags, places, people, events, captions)."""
    t0 = time.time()
    rows = conn.execute(
        """SELECT p.id, p.filename, p.folder, p.caption, p.camera_make, p.camera_model, p.source_kind,
                  p.description, p.ocr_text,
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
    text_payload = []      # only words written *in* or *about* the photo: OCR and descriptions
    for r in rows:
        parts = [
            r["filename"].rsplit(".", 1)[0].replace("_", " ").replace("-", " "),
            r["folder"].replace("/", " ").replace("_", " ").replace("-", " "),
            r["caption"] or "", r["place_name"] or "", r["city"] or "", r["admin1"] or "", r["country"] or "",
            r["landmark"] or "", r["user_title"] or r["auto_title"] or "",
            r["camera_make"] or "", r["camera_model"] or "", r["source_kind"] or "",
            " ".join(tag_map.get(int(r["id"]), [])), " ".join(people_map.get(int(r["id"]), [])),
            r["description"] or "", r["ocr_text"] or "",
        ]
        payload.append((int(r["id"]), " ".join(p for p in parts if p)))
        written = " ".join(x for x in (r["description"], r["ocr_text"]) if x)
        if written:
            text_payload.append((int(r["id"]), written))
    changed = _sync_fts(conn, "photo_fts", payload, batch) + _sync_fts(conn, "photo_text_fts", text_payload, batch)
    conn.commit()
    if changed:
        db.bump_generation(conn, "fts")
        conn.commit()
    return {"indexed": len(payload), "changed": changed, "seconds": round(time.time() - t0, 2)}


def _sync_fts(conn: sqlite3.Connection, table: str, payload: list[tuple[int, str]], batch: int) -> int:
    """Make `table` hold exactly `payload`, writing only the rows that differ. Deleting and inserting every row
    rewrote the whole index on every run (every hour), even when one photo had changed."""
    have = dict(conn.execute(f"SELECT rowid, text FROM {table}"))
    want = dict(payload)
    stale = [(rid,) for rid, text in have.items() if want.get(rid) != text]
    fresh = [(rid, text) for rid, text in want.items() if have.get(rid) != text]
    for i in range(0, len(stale), batch):
        conn.executemany(f"DELETE FROM {table} WHERE rowid = ?", stale[i:i + batch])
    for i in range(0, len(fresh), batch):
        conn.executemany(f"INSERT INTO {table}(rowid, text) VALUES (?,?)", fresh[i:i + batch])
    return len(stale) + len(fresh)
