"""Stacks: several files of one moment shown as a single item in the timeline.

* RAW + JPEG/HEIC — the same shot saved twice by the camera (same folder, same name).
  The viewable JPEG/HEIC is the cover.
* Bursts — consecutive frames from one camera, under BURST_GAP_S apart, that also look
  alike (pHash), so two unrelated quick snaps are not folded together. The cover is the
  best frame: the user's rating first, then the quality score.

Stacking only folds the *timeline* (`stack_hidden`); every frame stays searchable, its
faces still count, and duplicates still see it. A stack the user splits stays split.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from collections import defaultdict

from .. import db
from ..config import RAW_EXTENSIONS
from ..hashing import hamming
from ..vectors import load_photo_embeddings, normalize

log = logging.getLogger(__name__)

BURST_GAP_S = 1.5             # frames of one burst are fractions of a second apart
BURST_MAX_PHASH = 18          # a burst is one scene; reframing moves pHash only a little...
BURST_MIN_SIMILARITY = 0.93   # ...but pHash of a flat, noisy frame is unstable, so the embedding
                              # must agree too (the duplicate finder's "similar" bar). Neither
                              # value is measured on real bursts — see HANDOFF.md.
EXIF_SOURCES = ("exif", "exif_digitized", "xmp")
VIEWABLE_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp", ".avif"}


def _key(members: list[int]) -> str:
    return hashlib.sha1(",".join(map(str, sorted(members))).encode()).hexdigest()[:16]


def dismissed(conn: sqlite3.Connection) -> set[str]:
    return set(json.loads(db.get_meta(conn, "stacks_dismissed", "[]") or "[]"))


def build_stacks(conn: sqlite3.Connection, enabled: bool = True) -> dict:
    t0 = time.time()
    conn.execute("UPDATE photos SET stack_id = NULL, stack_hidden = 0 WHERE stack_id IS NOT NULL")
    if not enabled:
        conn.commit()
        return {"stacks": 0, "status": "disabled"}
    rows = conn.execute(
        """SELECT id, root_id, folder, filename, ext, taken_ts, date_source, camera_model, phash,
                  quality_score, rating, source_kind
           FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 AND media_type = 'image'
           ORDER BY taken_ts, id""").fetchall()
    skip = dismissed(conn)
    vec = _embeddings(conn)
    stacks: list[tuple[str, list]] = []
    in_stack: set[int] = set()

    # RAW + JPEG pairs
    by_name: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_name[(r["root_id"], r["folder"], os.path.splitext(r["filename"])[0].lower())].append(r)
    for group in by_name.values():
        raws = [r for r in group if r["ext"] in RAW_EXTENSIONS]
        views = [r for r in group if r["ext"] in VIEWABLE_EXT]
        if raws and views:
            members = views[:1] + raws
            stacks.append(("raw", members))
            in_stack.update(m["id"] for m in members)

    # bursts
    run: list = []

    def close_run():
        if len(run) >= 2:
            stacks.append(("burst", list(run)))
            in_stack.update(m["id"] for m in run)
        run.clear()

    for r in rows:
        if (r["id"] in in_stack or r["taken_ts"] is None or r["date_source"] not in EXIF_SOURCES
                or r["source_kind"] not in ("camera", "phone")):
            close_run()
            continue
        if run:
            prev = run[-1]
            same = (r["camera_model"] == prev["camera_model"]
                    and r["taken_ts"] - prev["taken_ts"] <= BURST_GAP_S
                    and prev["phash"] is not None and r["phash"] is not None
                    and hamming(prev["phash"], r["phash"]) <= BURST_MAX_PHASH
                    and _similar(vec, prev["id"], r["id"]))
            if not same:
                close_run()
        run.append(r)
    close_run()

    made = 0
    for kind, members in stacks:
        ids = [m["id"] for m in members]
        if _key(ids) in skip:
            continue
        if kind == "raw":
            cover = members[0]
        else:
            cover = max(members, key=lambda m: (m["rating"] or 0, m["quality_score"] or 0, -m["id"]))
        conn.executemany("UPDATE photos SET stack_id = ?, stack_hidden = ? WHERE id = ?",
                         [(cover["id"], 0 if m["id"] == cover["id"] else 1, m["id"]) for m in members])
        made += 1
    conn.commit()
    out = {"stacks": made, "seconds": round(time.time() - t0, 2)}
    log.info("Stacks: %s", out)
    return out


def _embeddings(conn: sqlite3.Connection) -> dict:
    model_id = db.active_model_id(conn, "semantic")
    if not model_id:
        return {}
    ids, mat = load_photo_embeddings(conn, model_id)
    mat = normalize(mat)
    return {int(i): mat[n] for n, i in enumerate(ids)}


def _similar(vec: dict, a: int, b: int) -> bool:
    if a not in vec or b not in vec:
        return False        # without embeddings there is no second opinion: do not stack
    return float(vec[a] @ vec[b]) >= BURST_MIN_SIMILARITY


def members(conn: sqlite3.Connection, stack_id: int) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT id FROM photos WHERE stack_id = ? ORDER BY stack_hidden, taken_ts, id", (stack_id,))]


def set_cover(conn: sqlite3.Connection, photo_id: int) -> None:
    row = conn.execute("SELECT stack_id FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None or row[0] is None:
        raise ValueError("photo is not in a stack")
    old = int(row[0])
    conn.execute("UPDATE photos SET stack_id = ?, stack_hidden = CASE WHEN id = ? THEN 0 ELSE 1 END "
                 "WHERE stack_id = ?", (photo_id, photo_id, old))
    db.audit(conn, "stack_cover", "photo", photo_id, {"previous": old})
    conn.commit()


def unstack(conn: sqlite3.Connection, stack_id: int) -> None:
    """Split a stack for good: remembered, so the next rebuild leaves these files alone."""
    ids = members(conn, stack_id)
    skip = dismissed(conn)
    skip.add(_key(ids))
    db.set_meta(conn, "stacks_dismissed", json.dumps(sorted(skip)))
    conn.execute("UPDATE photos SET stack_id = NULL, stack_hidden = 0 WHERE stack_id = ?", (stack_id,))
    db.audit(conn, "stack_split", "photo", stack_id, {"members": ids})
    conn.commit()
