"""Duplicate and near-duplicate detection.

Nothing is ever deleted or modified — groups are surfaced for manual review with
a suggested "keep" candidate and an explicit relation label.

Candidate generation (cheap, recall-oriented):
  * identical SHA-256                                  -> exact duplicates
  * shared 16-bit pHash band (pigeonhole: catches every pair within Hamming 3)
  * semantic kNN (catches crops, filters, screenshots that pHash misses)
Classification (precise): pHash/dHash distance + embedding similarity + EXIF.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from .. import db
from ..hashing import to_unsigned64
from ..vectors import knn, load_photo_embeddings, normalize

log = logging.getLogger(__name__)

KIND_RANK = {"exact": 3, "near": 2, "likely": 1, "similar": 0}


# Screenshots, documents and memes share heavy visual structure (app chrome, white
# backgrounds, text layout), so they need much stricter thresholds than photographs.
SYNTHETIC_KINDS = {"screenshot", "download"}


@dataclass
class DupParams:
    phash_near: int = 6
    dhash_near: int = 10
    phash_likely: int = 16
    sem_near: float = 0.985
    sem_likely: float = 0.955
    sem_similar: float = 0.93
    burst_seconds: float = 180.0
    knn_k: int = 8
    max_bucket: int = 400
    phash_synthetic: int = 2
    dhash_synthetic: int = 4
    sem_synthetic: float = 0.995


class _UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _popcount64(x: np.ndarray) -> np.ndarray:
    table = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    return table[x.view(np.uint8).reshape(-1, 8)].sum(axis=1).astype(np.int16)


def _hamming(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return _popcount64(np.bitwise_xor(a.astype(np.uint64), b.astype(np.uint64)))


def find_duplicates(ctx, conn: sqlite3.Connection, params: DupParams | None = None, progress=None) -> dict:
    p = params or DupParams()
    t0 = time.time()
    rows = conn.execute(
        "SELECT id, sha256, phash, dhash, width, height, size, taken_ts, date_source, source_kind, "
        "camera_model, quality_score, blur, first_seen_at, folder FROM photos WHERE status = 'ok'"
    ).fetchall()
    if not rows:
        return {"groups": 0, "new_groups": 0}
    n = len(rows)
    ids = np.array([r["id"] for r in rows], dtype=np.int64)
    row_of = {int(pid): i for i, pid in enumerate(ids)}
    phash = np.array([to_unsigned64(r["phash"]) if r["phash"] is not None else 0 for r in rows], dtype=np.uint64)
    dhash = np.array([to_unsigned64(r["dhash"]) if r["dhash"] is not None else 0 for r in rows], dtype=np.uint64)
    has_hash = np.array([r["phash"] is not None for r in rows])
    width = np.array([r["width"] or 0 for r in rows], dtype=np.int64)
    height = np.array([r["height"] or 0 for r in rows], dtype=np.int64)
    fsize = np.array([r["size"] or 0 for r in rows], dtype=np.int64)
    taken = np.array([r["taken_ts"] if r["taken_ts"] is not None else np.nan for r in rows], dtype=np.float64)
    date_src = [r["date_source"] for r in rows]
    source_kind = [r["source_kind"] for r in rows]
    camera = [r["camera_model"] for r in rows]

    pairs: dict[tuple[int, int], dict] = {}

    def add_pair(i: int, j: int) -> None:
        if i == j:
            return
        key = (i, j) if i < j else (j, i)
        if key not in pairs:
            pairs[key] = {}

    # ---- exact duplicates
    by_sha: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if r["sha256"]:
            by_sha[r["sha256"]].append(i)
    exact_pairs = set()
    for sha, members in by_sha.items():
        if len(members) > 1:
            for a in range(len(members)):
                for b in range(a + 1, len(members)):
                    key = (members[a], members[b])
                    exact_pairs.add(key)
                    add_pair(*key)

    # ---- pHash band candidates
    band_hits = 0
    for band in range(4):
        shift = np.uint64(band * 16)
        keys = (phash >> shift) & np.uint64(0xFFFF)
        order = np.argsort(keys, kind="stable")
        sorted_keys = keys[order]
        starts = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1]])
        ends = np.r_[starts[1:], len(order)]
        for s, e in zip(starts, ends):
            group = order[s:e]
            group = group[has_hash[group]]
            if len(group) < 2:
                continue
            if len(group) > p.max_bucket:
                # Degenerate hash (flat/black images): comparing all pairs would explode.
                log.debug("Skipping oversized pHash bucket (%d members)", len(group))
                continue
            for a in range(len(group)):
                for b in range(a + 1, len(group)):
                    i, j = int(group[a]), int(group[b])
                    if _hamming(phash[i:i + 1], phash[j:j + 1])[0] <= p.phash_likely:
                        add_pair(i, j)
                        band_hits += 1

    # ---- semantic neighbours (crops, filters, screenshots of photos)
    model_id = db.active_model_id(conn, "semantic")
    sem_sim: dict[tuple[int, int], float] = {}
    if model_id:
        emb_ids, mat = load_photo_embeddings(conn, model_id)
        if len(emb_ids):
            mat = normalize(mat)
            k = min(p.knn_k, len(emb_ids) - 1) if len(emb_ids) > 1 else 0
            if k > 0:
                idx, sims = knn(mat, mat, k, device=getattr(ctx, "device", "auto"), exclude_self=True)
                for a in range(len(emb_ids)):
                    ia = row_of.get(int(emb_ids[a]))
                    if ia is None:
                        continue
                    for c in range(idx.shape[1]):
                        s = float(sims[a, c])
                        if s < p.sem_similar:
                            continue
                        ib = row_of.get(int(emb_ids[idx[a, c]]))
                        if ib is None or ia == ib:
                            continue
                        key = (ia, ib) if ia < ib else (ib, ia)
                        sem_sim[key] = max(sem_sim.get(key, 0.0), s)
                        add_pair(ia, ib)

    # ---- classify pairs
    classified: list[tuple[int, int, str, float, int]] = []
    for (i, j) in pairs:
        ph = int(_hamming(phash[i:i + 1], phash[j:j + 1])[0]) if has_hash[i] and has_hash[j] else 64
        dh = int(_hamming(dhash[i:i + 1], dhash[j:j + 1])[0]) if has_hash[i] and has_hash[j] else 64
        sem = sem_sim.get((i, j), None)
        both_synthetic = (source_kind[i] in SYNTHETIC_KINDS and source_kind[j] in SYNTHETIC_KINDS)
        kind = None
        if (i, j) in exact_pairs:
            kind = "exact"
        elif both_synthetic:
            # Two screenshots/documents of the same app look alike by construction
            # (same chrome, same fonts). Only near-pixel-identical ones are duplicates.
            if ph <= p.phash_synthetic and dh <= p.dhash_synthetic and (width[i], height[i]) == (width[j], height[j]):
                kind = "near"
            elif sem is not None and sem >= p.sem_synthetic and ph <= p.phash_synthetic + 4:
                kind = "likely"
        elif (ph <= p.phash_near and dh <= p.dhash_near) or (sem is not None and sem >= p.sem_near and ph <= 12):
            kind = "near"
        elif sem is not None and sem >= p.sem_likely and (ph <= p.phash_likely
                                                          or "screenshot" in (source_kind[i], source_kind[j])
                                                          or date_src[i] != date_src[j]):
            kind = "likely"
        elif sem is not None and sem >= p.sem_similar:
            kind = "similar"
        if kind is None:
            continue
        if kind in ("likely", "near") and _is_burst(i, j, taken, camera, source_kind, date_src, ph, dh,
                                                    (i, j) in exact_pairs, p):
            kind = "similar"
        classified.append((i, j, kind, float(sem) if sem is not None else _sem_from_hash(ph), ph))

    # ---- group by kind with union-find (a group only links pairs of the same or stronger kind)
    groups: dict[str, list[list[int]]] = {}
    for kind in ("exact", "near", "likely", "similar"):
        uf = _UnionFind()
        rank = KIND_RANK[kind]
        members_used = set()
        for (i, j, k, sem, ph) in classified:
            if KIND_RANK[k] >= rank:
                uf.union(i, j)
                members_used.update((i, j))
        buckets: dict[int, list[int]] = defaultdict(list)
        for m in members_used:
            buckets[uf.find(m)].append(m)
        groups[kind] = [sorted(v) for v in buckets.values() if len(v) > 1]

    # Keep only the strongest kind for each set of photos: a group is emitted at kind K
    # if it is not already fully contained in a stronger group.
    emitted: list[tuple[str, list[int]]] = []
    covered: list[set[int]] = []
    for kind in ("exact", "near", "likely", "similar"):
        for members in groups[kind]:
            ms = set(members)
            if any(ms <= c for c in covered):
                continue
            emitted.append((kind, members))
            covered.append(ms)

    pair_kind = {(i, j): (k, sem, ph) for (i, j, k, sem, ph) in classified}
    total_groups, new_groups = _write_groups(conn, emitted, ids, rows, pair_kind, row_of)
    conn.commit()
    db.bump_generation(conn, "duplicates")
    conn.commit()
    out = {"photos": n, "pairs": len(classified), "groups": total_groups, "new_groups": new_groups,
           "by_kind": {k: sum(1 for kk, _ in emitted if kk == k) for k in KIND_RANK},
           "seconds": round(time.time() - t0, 2)}
    log.info("Duplicate detection: %s", out)
    return out


def _sem_from_hash(ph: int) -> float:
    return max(0.0, 1.0 - ph / 64.0)


EXIF_DATE_SOURCES = ("exif", "exif_digitized", "xmp")


def _is_burst(i: int, j: int, taken, camera, source_kind, date_src, ph: int, dh: int, sha_equal: bool,
              p: DupParams) -> bool:
    """Consecutive shots of the same scene are 'similar', not duplicates.

    The decisive signal is the *capture* timestamp: two frames a second apart are
    two separate exposures, while a copy/resave inherits the original's
    DateTimeOriginal and so has a zero delta. Pixel distance cannot separate them
    — a burst pair can be within 2 bits of pHash.
    """
    if sha_equal:
        return False
    if np.isnan(taken[i]) or np.isnan(taken[j]):
        return False
    if date_src[i] not in EXIF_DATE_SOURCES or date_src[j] not in EXIF_DATE_SOURCES:
        return False
    dt = abs(taken[i] - taken[j])
    if dt < 0.5 or dt > p.burst_seconds:
        return False
    return source_kind[i] in ("camera", "phone") and source_kind[j] in ("camera", "phone")


def _relation(i: int, j: int, keep: int, rows, ph: int, sem: float) -> str:
    """Describe the non-keeper's relation to the kept photo."""
    other = j if keep == i else i
    a, b = rows[keep], rows[other]
    if a["sha256"] and a["sha256"] == b["sha256"]:
        return "exact"
    if b["source_kind"] == "screenshot" and a["source_kind"] != "screenshot":
        return "screenshot"
    aw, ah = a["width"] or 0, a["height"] or 0
    bw, bh = b["width"] or 0, b["height"] or 0
    if aw and bw:
        ar_a = aw / max(ah, 1)
        ar_b = bw / max(bh, 1)
        if abs(ar_a - ar_b) > 0.02 * max(ar_a, 1):
            return "cropped"
        if (bw, bh) != (aw, ah):
            return "resized"
    if ph > 4:
        return "edited"
    if (b["size"] or 0) < (a["size"] or 0):
        return "compressed"
    return "duplicate"


# Folder names that suggest a secondary copy rather than the working original.
SECONDARY_FOLDER_WORDS = ("backup", "back up", "copy", "copies", "duplicate", "archive", "old",
                          "recycle", "trash", "temp", "tmp", "export", "compressed", "resized",
                          "whatsapp", "telegram", "downloads", "download", "recovered")


def _keep_candidate(members: list[int], rows) -> int:
    """Suggest which copy to keep: the most original, highest-resolution one.

    For byte-identical copies everything above is equal, so the tie is broken on
    where the file lives: a photo in Backup/ or Copies/ is the derivative, and a
    shallower path is more likely to be the one the user actually browses.
    """
    def score(i: int) -> tuple:
        r = rows[i]
        origin = {"camera": 4, "phone": 4, "scan": 3, "edited": 2, "download": 1, "whatsapp": 0,
                  "screenshot": -1}.get(r["source_kind"], 1)
        has_exif_date = 1 if r["date_source"] in ("exif", "exif_digitized", "xmp") else 0
        pixels = (r["width"] or 0) * (r["height"] or 0)
        folder = (r["folder"] or "").lower()
        primary = 0 if any(w in folder for w in SECONDARY_FOLDER_WORDS) else 1
        depth = -folder.count("/")
        return (origin, has_exif_date, pixels, r["size"] or 0, primary, depth, -(r["first_seen_at"] or 0))
    return max(members, key=score)


def _write_groups(conn, emitted, ids, rows, pair_kind, row_of) -> tuple[int, int]:
    now = time.time()
    existing = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT signature, id, review_status FROM dup_groups")}
    seen_signatures = set()
    written = 0
    for kind, members in emitted:
        photo_ids = sorted(int(ids[m]) for m in members)
        signature = hashlib.sha1((kind + ":" + ",".join(map(str, photo_ids))).encode()).hexdigest()
        seen_signatures.add(signature)
        keep_row = _keep_candidate(members, rows)
        keep_id = int(ids[keep_row])
        if signature in existing:
            gid = existing[signature][0]
            conn.execute("UPDATE dup_groups SET member_count=?, keep_photo_id=?, updated_at=? WHERE id=?",
                         (len(photo_ids), keep_id, now, gid))
        else:
            cur = conn.execute(
                "INSERT INTO dup_groups(kind, member_count, keep_photo_id, signature, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)", (kind, len(photo_ids), keep_id, signature, now, now))
            gid = int(cur.lastrowid)
            written += 1
        conn.execute("DELETE FROM dup_members WHERE group_id=?", (gid,))
        for m in members:
            pid = int(ids[m])
            key = (min(m, keep_row), max(m, keep_row))
            info = pair_kind.get(key)
            sem = info[1] if info else 1.0
            ph = info[2] if info else 0
            rel = "original" if pid == keep_id else _relation(keep_row, m, keep_row, rows, ph, sem)
            conn.execute(
                "INSERT OR REPLACE INTO dup_members(group_id, photo_id, relation, similarity, hamming) "
                "VALUES (?,?,?,?,?)", (gid, pid, rel, float(sem), int(ph)))
    stale = [sig for sig in existing if sig not in seen_signatures]
    for sig in stale:
        conn.execute("DELETE FROM dup_groups WHERE signature = ?", (sig,))
    return len(seen_signatures), written
