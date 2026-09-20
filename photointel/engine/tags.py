"""Zero-shot semantic tagging and the aesthetic proxy score.

Scoring
-------
SigLIP's absolute match probability for a short prompt is tiny (p99 ~0.001 on
COCO), so an absolute cut-off is meaningless. Instead each tag score is the
*minimum* of two standardised views of the same similarity:

  * per-tag z: how unusual this score is for this tag across the library
    (removes each prompt's intrinsic bias)
  * per-photo z: how much this tag stands out among all tags for this photo
    (stops "everything is slightly a city street" noise)

Measured on COCO instance annotations (eval/calibrate_tags.py, 1500 images,
16 object categories): z>=1.5 -> P 0.46 / R 0.81; z>=2.0 -> P 0.68 / R 0.70;
z>=2.5 -> P 0.85 / R 0.56. We store at 1.5 (recall) and filter at higher z
where precision matters (search filters, event categories).

Tags are derived from the stored image embeddings — no second pass over pixels —
so re-tagging the whole library after a vocabulary change is one matrix multiply.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import numpy as np

from .. import db, quality
from ..vectors import load_photo_embeddings, normalize
from ..vision.vocab import (AESTHETIC_NEGATIVE, AESTHETIC_POSITIVE, VOCAB_VERSION, all_tags)

log = logging.getLogger(__name__)

MAX_TAGS_PER_PHOTO = 12
TAG_MIN_Z = 1.5          # storage threshold (recall-oriented)
TAG_FILTER_Z = 2.0       # "this tag really applies" (search filters, event categories)
TAG_STRONG_Z = 2.5       # high-precision uses
STATS_SAMPLE = 20000


def tag_vectors(model) -> tuple[list[tuple[str, str]], np.ndarray]:
    """Mean-of-prompts text embedding per tag (prompt ensembling)."""
    entries = all_tags()
    prompts: list[str] = []
    spans: list[tuple[int, int]] = []
    for _, _, ps in entries:
        spans.append((len(prompts), len(prompts) + len(ps)))
        prompts.extend(ps)
    emb = model.encode_texts(prompts)
    vecs = np.stack([normalize(emb[a:b].mean(0, keepdims=True))[0] for a, b in spans])
    return [(t, c) for t, c, _ in entries], vecs


def ensure_tag_rows(conn: sqlite3.Connection, names: list[tuple[str, str]]) -> dict[str, int]:
    existing = {r[0]: int(r[1]) for r in conn.execute("SELECT name, id FROM tags")}
    for name, category in names:
        if name not in existing:
            cur = conn.execute("INSERT INTO tags(name, category) VALUES (?,?)", (name, category))
            existing[name] = int(cur.lastrowid)
        else:
            conn.execute("UPDATE tags SET category=? WHERE id=?", (category, existing[name]))
    conn.commit()
    return existing


def _tag_stats(conn: sqlite3.Connection, mat: np.ndarray, tvecs: np.ndarray, model_id: int,
               force: bool) -> tuple[np.ndarray, np.ndarray]:
    """Per-tag mean/std of similarity across the library (cached so incremental runs agree)."""
    key = f"tag_stats:{model_id}:{VOCAB_VERSION}"
    cached = db.get_meta(conn, key)
    if cached and not force:
        try:
            data = json.loads(cached)
            mu = np.array(data["mu"], dtype=np.float32)
            sd = np.array(data["sd"], dtype=np.float32)
            if mu.shape[0] == tvecs.shape[0]:
                return mu[None, :], sd[None, :]
        except Exception:
            log.debug("bad cached tag stats", exc_info=True)
    sample = mat
    if len(mat) > STATS_SAMPLE:
        idx = np.random.default_rng(11).choice(len(mat), STATS_SAMPLE, replace=False)
        sample = mat[idx]
    sims = sample @ tvecs.T
    mu = sims.mean(axis=0)
    sd = sims.std(axis=0) + 1e-6
    db.set_meta(conn, key, json.dumps({"mu": [round(float(x), 6) for x in mu],
                                       "sd": [round(float(x), 6) for x in sd],
                                       "n": int(len(sample))}))
    conn.commit()
    return mu[None, :].astype(np.float32), sd[None, :].astype(np.float32)


def tag_photos(ctx, conn: sqlite3.Connection, force: bool = False, batch: int = 4096, progress=None) -> dict:
    model_id = db.active_model_id(conn, "semantic")
    if model_id is None:
        return {"status": "no semantic model"}
    stored_version = db.get_meta(conn, "tag_vocab_version")
    stored_model = db.get_meta(conn, "tag_model_id")
    stale = force or stored_version != str(VOCAB_VERSION) or stored_model != str(model_id)

    ids, mat = load_photo_embeddings(conn, model_id)
    if len(ids) == 0:
        return {"tagged": 0}
    mat = normalize(mat)
    t0 = time.time()
    model = ctx.semantic_model()
    names, tvecs = tag_vectors(model)
    tag_ids = ensure_tag_rows(conn, names)
    mu, sd = _tag_stats(conn, mat, tvecs, model_id, force=stale)

    todo_ids, todo_mat = ids, mat
    if not stale:
        done = {int(r[0]) for r in conn.execute("SELECT DISTINCT photo_id FROM photo_tags")}
        keep = np.array([i for i, pid in enumerate(ids) if int(pid) not in done], dtype=np.int64)
        if len(keep) == 0:
            return {"tagged": 0, "status": "up to date"}
        todo_ids, todo_mat = ids[keep], mat[keep]

    pos = model.encode_texts(AESTHETIC_POSITIVE)
    neg = model.encode_texts(AESTHETIC_NEGATIVE)
    if stale:
        conn.execute("DELETE FROM photo_tags WHERE source = 'semantic'")
        conn.commit()

    written = 0
    for start in range(0, len(todo_ids), batch):
        chunk_ids = todo_ids[start:start + batch]
        chunk = todo_mat[start:start + batch]
        sims = chunk @ tvecs.T
        z_tag = (sims - mu) / sd
        z_photo = (sims - sims.mean(axis=1, keepdims=True)) / (sims.std(axis=1, keepdims=True) + 1e-6)
        score = np.minimum(z_tag, z_photo)
        rows = []
        for r in range(len(chunk_ids)):
            s = score[r]
            cand = np.flatnonzero(s >= TAG_MIN_Z)
            if len(cand) > MAX_TAGS_PER_PHOTO:
                cand = cand[np.argsort(-s[cand])[:MAX_TAGS_PER_PHOTO]]
            for c in cand:
                rows.append((int(chunk_ids[r]), tag_ids[names[c][0]], round(float(s[c]), 3), "semantic"))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO photo_tags(photo_id, tag_id, score, source) VALUES (?,?,?,?)", rows)
            written += len(rows)
        ap = (chunk @ pos.T).mean(axis=1)
        an = (chunk @ neg.T).mean(axis=1)
        aesthetic = 1.0 / (1.0 + np.exp(-(ap - an) * 30.0))
        conn.executemany("UPDATE photos SET aesthetic=? WHERE id=?",
                         [(float(a), int(p)) for a, p in zip(aesthetic, chunk_ids)])
        conn.commit()
        if progress:
            progress(min(start + batch, len(todo_ids)), len(todo_ids))

    db.set_meta(conn, "tag_vocab_version", VOCAB_VERSION)
    db.set_meta(conn, "tag_model_id", model_id)
    conn.commit()
    out = {"photos": len(todo_ids), "tags_written": written,
           "tags_per_photo": round(written / max(len(todo_ids), 1), 2), "seconds": round(time.time() - t0, 2)}
    log.info("Semantic tagging: %s", out)
    return out


def recompute_quality(conn: sqlite3.Connection, only_missing: bool = True) -> dict:
    """Combine sharpness/exposure/resolution/aesthetic/faces into photos.quality_score."""
    sql = ("SELECT p.id, p.blur, p.brightness, p.contrast, p.clipped, p.width, p.height, p.aesthetic, "
           "p.source_kind, (SELECT MAX(f.quality) FROM faces f WHERE f.photo_id = p.id) AS face_q "
           "FROM photos p WHERE p.status='ok'")
    if only_missing:
        sql += " AND p.quality_score IS NULL"
    rows = conn.execute(sql).fetchall()
    updates = []
    for r in rows:
        score = quality.combined_quality(
            {"blur": r["blur"], "brightness": r["brightness"], "contrast": r["contrast"], "clipped": r["clipped"],
             "width": r["width"], "height": r["height"], "source_kind": r["source_kind"]},
            face_quality=r["face_q"], aesthetic=r["aesthetic"])
        updates.append((score, r["id"]))
    conn.executemany("UPDATE photos SET quality_score=? WHERE id=?", updates)
    conn.commit()
    return {"scored": len(updates)}


def top_tags_for_photos(conn: sqlite3.Connection, photo_ids: list[int], limit: int = 8,
                        min_score: float = TAG_FILTER_Z) -> list[tuple[str, float]]:
    if not photo_ids:
        return []
    photo_ids = photo_ids[:900]
    q = ",".join("?" * len(photo_ids))
    rows = conn.execute(
        f"""SELECT t.name, AVG(pt.score) AS s, COUNT(*) AS n FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
            WHERE pt.photo_id IN ({q}) AND pt.score >= ? GROUP BY t.id
            ORDER BY (COUNT(*) * AVG(pt.score)) DESC LIMIT ?""",
        (*photo_ids, min_score, limit)).fetchall()
    return [(r["name"], float(r["s"])) for r in rows]


def confidence(score: float) -> float:
    """Map a z-score to a 0..1 display confidence."""
    return float(min(1.0, max(0.0, (score - 1.0) / 3.0)))
