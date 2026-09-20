"""Person identity management.

Guarantees
----------
* A face the user assigned by hand is never reassigned by the algorithm.
* A (face, person) pair the user rejected is never proposed again.
* Named people keep their identity across re-clustering and across face-model
  upgrades; ids are stable so links/bookmarks keep working.
* Every destructive or corrective operation is written to `audit_log`.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter, defaultdict

import numpy as np

from .. import db
from ..vectors import load_face_embeddings, normalize
from .clustering import ClusterParams, attach_faces, cluster_faces, suggest_merges

log = logging.getLogger(__name__)


def person_label(row) -> str:
    if row["name"]:
        return row["name"]
    no = row["display_no"] if "display_no" in row.keys() else None
    return f"Person {no:03d}" if no else f"Person {row['id']:03d}"


def _next_display_no(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(display_no), 0) FROM persons").fetchone()
    return int(row[0]) + 1


def create_person(conn: sqlite3.Connection, name: str | None = None, face_model: int | None = None,
                  actor: str = "system") -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO persons(name, display_no, face_model, created_at, updated_at) VALUES (?,?,?,?,?)",
        (name, _next_display_no(conn), face_model, now, now),
    )
    pid = int(cur.lastrowid)
    db.audit(conn, "person_created", "person", pid, {"name": name}, actor=actor)
    if name:
        db.bump_generation(conn, "people")   # a named person must be searchable at once
    return pid


def _rejections(conn: sqlite3.Connection, face_ids: np.ndarray) -> dict[int, set[int]]:
    out: dict[int, set[int]] = defaultdict(set)
    for fid, pid in conn.execute("SELECT face_id, person_id FROM face_rejections"):
        out[int(fid)].add(int(pid))
    return out


def recluster(ctx, conn: sqlite3.Connection, full: bool = False, params: ClusterParams | None = None,
              progress=None) -> dict:
    """Assign faces to people.

    full=False (default): keep existing people, attach unassigned faces to them,
    and cluster whatever is left into new people. This is the incremental path
    used after every index run.

    full=True: re-derive clusters from scratch, then map them back onto existing
    people so names/ids survive.
    """
    t0 = time.time()
    p = params or ClusterParams()
    model_id = db.active_model_id(conn, "face")
    if model_id is None:
        return {"status": "no face model"}
    ids, mat, meta = load_face_embeddings(conn, model_id)
    if len(ids) == 0:
        return {"status": "no faces"}
    mat = normalize(mat)
    device = ctx.device if hasattr(ctx, "device") else "auto"
    row_of = {int(f): i for i, f in enumerate(ids)}
    rejections_by_face = _rejections(conn, ids)
    rejections_rows = {row_of[f]: pids for f, pids in rejections_by_face.items() if f in row_of}

    valid_persons = {int(r[0]) for r in conn.execute("SELECT id FROM persons WHERE merged_into IS NULL")}
    person_of_row = meta["person_id"].copy()
    for i, pid in enumerate(person_of_row):
        if pid >= 0 and int(pid) not in valid_persons:
            person_of_row[i] = -1
    user_locked = meta["user_assigned"]
    stats = {"faces": len(ids), "new_people": 0, "attached": 0, "clustered": 0}

    if full or not valid_persons:
        result = cluster_faces(mat, meta, p, device=device)
        stats["clustered"] = int((result.labels >= 0).sum())
        assignments = _map_clusters_to_persons(conn, ids, mat, result, person_of_row, user_locked,
                                               rejections_rows, model_id, stats)
    else:
        assignments = {}
        identity_rows: dict[int, np.ndarray] = {}
        for pid in valid_persons:
            rows = np.flatnonzero(person_of_row == pid)
            if len(rows):
                identity_rows[pid] = rows
        unassigned = np.flatnonzero(person_of_row < 0)
        if len(unassigned):
            strong = np.flatnonzero((person_of_row < 0) & _strong_mask(meta, p))
            attached = attach_faces(mat, strong, identity_rows, p, device=device, forbidden=rejections_rows)
            for row, (pid, conf) in attached.items():
                assignments[row] = (pid, "attach", conf)
            stats["attached"] = len(attached)
            # Cluster whatever still has no identity into new people.
            left = np.array([r for r in unassigned if r not in assignments], dtype=np.int64)
            if len(left) >= p.min_cluster_size:
                sub_meta = {k: v[left] for k, v in meta.items()}
                res = cluster_faces(mat[left], sub_meta, p, device=device)
                for lab in range(res.n_clusters):
                    rows = left[np.flatnonzero(res.labels == lab)]
                    new_pid = create_person(conn, None, face_model=model_id, actor="clustering")
                    stats["new_people"] += 1
                    for r in rows:
                        assignments[int(r)] = (new_pid, "cluster", float(res.confidence[np.flatnonzero(left == r)[0]]))
                stats["clustered"] += int((res.labels >= 0).sum())

    # Weak faces: attach to whatever identities now exist (stricter margin already applied).
    identity_rows = _identity_rows_after(person_of_row, assignments)
    weak = [i for i in range(len(ids))
            if not user_locked[i] and i not in assignments
            and (person_of_row[i] < 0)]
    if weak and identity_rows:
        attached = attach_faces(mat, np.array(weak, dtype=np.int64), identity_rows, p, device=device,
                                forbidden=rejections_rows)
        for row, (pid, conf) in attached.items():
            assignments[row] = (pid, "attach", conf)
            stats["attached"] += 1

    # Persist
    updates = []
    for row, (pid, source, conf) in assignments.items():
        if user_locked[row]:
            continue
        if int(person_of_row[row]) == pid:
            continue
        updates.append((pid, source, float(conf), int(ids[row])))
    conn.executemany("UPDATE faces SET person_id=?, assign_source=?, assign_confidence=? WHERE id=?", updates)
    stats["updated_faces"] = len(updates)
    update_person_stats(conn)
    conn.commit()
    db.bump_generation(conn, "people")
    conn.commit()
    stats["seconds"] = round(time.time() - t0, 2)
    log.info("Reclustering complete: %s", stats)
    return stats


def _strong_mask(meta: dict, p: ClusterParams) -> np.ndarray:
    return ((meta["quality"] >= p.min_quality) & (meta["det_score"] >= p.min_det_score)
            & (meta["size_px"] >= p.min_size_px))


def _identity_rows_after(person_of_row: np.ndarray, assignments: dict) -> dict[int, np.ndarray]:
    groups: dict[int, list[int]] = defaultdict(list)
    for i, pid in enumerate(person_of_row):
        if pid >= 0:
            groups[int(pid)].append(i)
    for row, (pid, _, _) in assignments.items():
        groups[int(pid)].append(row)
    return {pid: np.array(rows, dtype=np.int64) for pid, rows in groups.items() if rows}


def _map_clusters_to_persons(conn, ids, mat, result, person_of_row, user_locked, rejections_rows,
                             model_id: int, stats: dict) -> dict:
    """Attach freshly-derived clusters to existing people, preserving user intent.

    A person id may continue into at most one cluster. Without that rule a full
    recluster can never repair an over-merge: an existing person holding faces
    that now belong to several distinct clusters would claim a majority in each
    one and silently re-merge them. The purpose here is to carry names and ids
    across, not to re-impose the previous grouping.
    """
    assignments: dict[int, tuple[int, str, float]] = {}
    # How many faces of each existing person a cluster holds — used to settle
    # competing claims so the id follows the bulk of that person's faces.
    claim_strength: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for lab in range(result.n_clusters):
        rows = np.flatnonzero(result.labels == lab)
        for pid, n in Counter(int(person_of_row[r]) for r in rows if person_of_row[r] >= 0).items():
            claim_strength[pid].append((n, lab))
    best_cluster_for = {pid: max(claims)[1] for pid, claims in claim_strength.items()}

    for lab in range(result.n_clusters):
        rows = np.flatnonzero(result.labels == lab)
        if len(rows) == 0:
            continue
        confirmed = Counter(int(person_of_row[r]) for r in rows if user_locked[r] and person_of_row[r] >= 0)
        weak = Counter(int(person_of_row[r]) for r in rows if not user_locked[r] and person_of_row[r] >= 0)
        conf_lab = result.cluster_confidence.get(lab, 0.5)
        if len(confirmed) > 1:
            # The cluster spans identities the user explicitly separated: split it by
            # nearest confirmed member instead of merging two real people.
            targets = list(confirmed)
            centroids = {}
            for pid in targets:
                sel = [r for r in rows if user_locked[r] and int(person_of_row[r]) == pid]
                centroids[pid] = normalize(mat[sel].mean(0, keepdims=True))[0]
            for r in rows:
                if user_locked[r]:
                    continue
                sims = {pid: float(mat[r] @ c) for pid, c in centroids.items()}
                pid = max(sims, key=sims.get)
                if pid in rejections_rows.get(int(r), set()):
                    continue
                assignments[int(r)] = (pid, "cluster", sims[pid])
            continue
        if confirmed and best_cluster_for.get(next(iter(confirmed))) == lab:
            pid = next(iter(confirmed))
        elif (weak and weak.most_common(1)[0][1] >= 0.5 * len(rows)
              and best_cluster_for.get(weak.most_common(1)[0][0]) == lab):
            pid = weak.most_common(1)[0][0]
        else:
            # Either nothing recognisable here, or this person's id has already
            # gone to the cluster holding more of their faces: start a new person.
            pid = create_person(conn, None, face_model=model_id, actor="clustering")
            stats["new_people"] += 1
        for r in rows:
            if user_locked[r]:
                continue
            if pid in rejections_rows.get(int(r), set()):
                continue
            assignments[int(r)] = (pid, "cluster", float(result.confidence[r]))
    return assignments


def update_person_stats(conn: sqlite3.Connection, person_ids: list[int] | None = None) -> None:
    where = ""
    args: tuple = ()
    if person_ids:
        where = f" WHERE p.id IN ({','.join('?' * len(person_ids))})"
        args = tuple(person_ids)
    conn.execute(
        f"""UPDATE persons SET
              face_count = COALESCE((SELECT COUNT(*) FROM faces f WHERE f.person_id = persons.id), 0),
              photo_count = COALESCE((SELECT COUNT(DISTINCT f.photo_id) FROM faces f
                                      JOIN photos ph ON ph.id = f.photo_id
                                      WHERE f.person_id = persons.id AND ph.status='ok'), 0),
              first_seen_ts = (SELECT MIN(ph.taken_ts) FROM faces f JOIN photos ph ON ph.id = f.photo_id
                               WHERE f.person_id = persons.id AND ph.status='ok'),
              last_seen_ts = (SELECT MAX(ph.taken_ts) FROM faces f JOIN photos ph ON ph.id = f.photo_id
                              WHERE f.person_id = persons.id AND ph.status='ok'),
              cluster_confidence = (SELECT AVG(f.assign_confidence) FROM faces f WHERE f.person_id = persons.id),
              updated_at = ?
            WHERE merged_into IS NULL{where.replace('p.id', 'persons.id')}""",
        (time.time(), *args),
    )
    # Cover face: the most recognisable large face (quality x size), preferring user-confirmed ones.
    conn.execute(
        f"""UPDATE persons SET cover_face_id = (
              SELECT f.id FROM faces f JOIN photos ph ON ph.id = f.photo_id
              WHERE f.person_id = persons.id AND ph.status = 'ok'
              ORDER BY (CASE WHEN f.assign_source='user' THEN 1 ELSE 0 END) DESC,
                       (f.quality * MIN(f.size_px, 400)) DESC LIMIT 1)
            WHERE merged_into IS NULL{where.replace('p.id', 'persons.id')}""",
        args,
    )


# ---------------------------------------------------------------- user operations

def rename_person(conn: sqlite3.Connection, person_id: int, name: str | None) -> None:
    old = conn.execute("SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
    conn.execute("UPDATE persons SET name=?, updated_at=? WHERE id=?", (name or None, time.time(), person_id))
    db.audit(conn, "person_renamed", "person", person_id, {"from": old[0] if old else None, "to": name})
    db.bump_generation(conn, "people")
    conn.commit()


def set_person_flags(conn: sqlite3.Connection, person_id: int, hidden: bool | None = None,
                     ignored: bool | None = None) -> None:
    sets, args = [], []
    if hidden is not None:
        sets.append("hidden=?")
        args.append(int(hidden))
    if ignored is not None:
        sets.append("ignored=?")
        args.append(int(ignored))
    if not sets:
        return
    args += [time.time(), person_id]
    conn.execute(f"UPDATE persons SET {', '.join(sets)}, updated_at=? WHERE id=?", args)
    db.audit(conn, "person_flags", "person", person_id, {"hidden": hidden, "ignored": ignored})
    db.bump_generation(conn, "people")
    conn.commit()


def merge_persons(conn: sqlite3.Connection, target_id: int, source_ids: list[int]) -> dict:
    """Move every face of `source_ids` onto `target_id`. Reversible via the audit log."""
    source_ids = [int(s) for s in source_ids if int(s) != int(target_id)]
    if not source_ids:
        return {"merged": 0}
    moved = 0
    for sid in source_ids:
        face_ids = [int(r[0]) for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (sid,))]
        conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (target_id, sid))
        conn.execute("UPDATE persons SET merged_into=?, updated_at=? WHERE id=?", (target_id, time.time(), sid))
        # Rejections follow the merge so the user's "not this person" still holds.
        conn.execute("UPDATE OR IGNORE face_rejections SET person_id=? WHERE person_id=?", (target_id, sid))
        conn.execute("DELETE FROM face_rejections WHERE person_id=?", (sid,))
        db.audit(conn, "person_merged", "person", target_id, {"source": sid, "faces": face_ids[:2000]})
        moved += len(face_ids)
    update_person_stats(conn)
    db.bump_generation(conn, "people")
    conn.commit()
    return {"merged": len(source_ids), "faces_moved": moved}


def split_person(conn: sqlite3.Connection, person_id: int, face_ids: list[int], name: str | None = None) -> dict:
    """Move the given faces out of a person into a new person (a mis-clustered identity)."""
    if not face_ids:
        return {"created": None}
    model_id = db.active_model_id(conn, "face")
    new_id = create_person(conn, name, face_model=model_id, actor="user")
    q = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE faces SET person_id=?, assign_source='user', assign_confidence=1.0 WHERE id IN ({q})",
                 (new_id, *face_ids))
    # The user asserted these are not the original person.
    now = time.time()
    conn.executemany("INSERT OR IGNORE INTO face_rejections(face_id, person_id, created_at) VALUES (?,?,?)",
                     [(int(f), int(person_id), now) for f in face_ids])
    conn.execute("INSERT OR IGNORE INTO person_not_same(a, b, created_at) VALUES (?,?,?)",
                 (min(person_id, new_id), max(person_id, new_id), now))
    db.audit(conn, "person_split", "person", person_id, {"new_person": new_id, "faces": face_ids[:2000]})
    update_person_stats(conn)
    db.bump_generation(conn, "people")
    conn.commit()
    return {"created": new_id, "faces": len(face_ids)}


def assign_faces(conn: sqlite3.Connection, face_ids: list[int], person_id: int | None = None,
                 name: str | None = None) -> dict:
    """Confirm an identity for faces (user action -> locked)."""
    if person_id is None:
        if name:
            row = conn.execute("SELECT id FROM persons WHERE name = ? AND merged_into IS NULL", (name,)).fetchone()
            person_id = int(row[0]) if row else create_person(conn, name, db.active_model_id(conn, "face"), actor="user")
        else:
            person_id = create_person(conn, None, db.active_model_id(conn, "face"), actor="user")
    prev = {int(r[0]): r[1] for r in conn.execute(
        f"SELECT id, person_id FROM faces WHERE id IN ({','.join('?' * len(face_ids))})", face_ids)}
    q = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE faces SET person_id=?, assign_source='user', assign_confidence=1.0 WHERE id IN ({q})",
                 (person_id, *face_ids))
    now = time.time()
    # Moving a face away from a person is also a rejection of the old assignment.
    rej = [(fid, int(old), now) for fid, old in prev.items() if old is not None and int(old) != int(person_id)]
    if rej:
        conn.executemany("INSERT OR IGNORE INTO face_rejections(face_id, person_id, created_at) VALUES (?,?,?)", rej)
    db.audit(conn, "faces_assigned", "person", person_id, {"faces": face_ids[:2000], "previous": {str(k): v for k, v in list(prev.items())[:2000]}})
    update_person_stats(conn)
    db.bump_generation(conn, "people")
    conn.commit()
    return {"person_id": person_id, "faces": len(face_ids)}


def reject_faces(conn: sqlite3.Connection, face_ids: list[int], person_id: int) -> dict:
    now = time.time()
    conn.executemany("INSERT OR IGNORE INTO face_rejections(face_id, person_id, created_at) VALUES (?,?,?)",
                     [(int(f), int(person_id), now) for f in face_ids])
    q = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE faces SET person_id=NULL, assign_source=NULL, assign_confidence=NULL "
                 f"WHERE id IN ({q}) AND person_id = ?", (*face_ids, person_id))
    db.audit(conn, "faces_rejected", "person", person_id, {"faces": face_ids[:2000]})
    update_person_stats(conn)
    db.bump_generation(conn, "people")
    conn.commit()
    return {"rejected": len(face_ids)}


def mark_not_same(conn: sqlite3.Connection, a: int, b: int) -> None:
    conn.execute("INSERT OR IGNORE INTO person_not_same(a, b, created_at) VALUES (?,?,?)",
                 (min(a, b), max(a, b), time.time()))
    db.audit(conn, "person_not_same", "person", a, {"other": b})
    conn.commit()


def merge_suggestions(ctx, conn: sqlite3.Connection, threshold: float = 0.50, limit: int = 20) -> list[dict]:
    model_id = db.active_model_id(conn, "face")
    if model_id is None:
        return []
    ids, mat, meta = load_face_embeddings(conn, model_id)
    if len(ids) == 0:
        return []
    mat = normalize(mat)
    named = {int(r[0]) for r in conn.execute("SELECT id FROM persons WHERE merged_into IS NULL AND ignored = 0")}
    rows_by_person: dict[int, list[int]] = defaultdict(list)
    for i, pid in enumerate(meta["person_id"]):
        if pid >= 0 and int(pid) in named:
            rows_by_person[int(pid)].append(i)
    identity_rows = {k: np.array(v) for k, v in rows_by_person.items() if len(v) >= 2}
    pairs = suggest_merges(mat, identity_rows, threshold=threshold, device=getattr(ctx, "device", "auto"))
    blocked = {(int(a), int(b)) for a, b in conn.execute("SELECT a, b FROM person_not_same")}
    out = []
    for a, b, score in pairs:
        if (min(a, b), max(a, b)) in blocked:
            continue
        ra = conn.execute("SELECT id, name, display_no, cover_face_id, photo_count FROM persons WHERE id=?", (a,)).fetchone()
        rb = conn.execute("SELECT id, name, display_no, cover_face_id, photo_count FROM persons WHERE id=?", (b,)).fetchone()
        if not ra or not rb:
            continue
        out.append({
            "a": {"id": a, "label": person_label(ra), "cover_face_id": ra["cover_face_id"], "photo_count": ra["photo_count"]},
            "b": {"id": b, "label": person_label(rb), "cover_face_id": rb["cover_face_id"], "photo_count": rb["photo_count"]},
            "score": round(float(score), 3),
        })
        if len(out) >= limit:
            break
    return out


def co_occurring(conn: sqlite3.Connection, person_id: int, limit: int = 8) -> list[dict]:
    rows = conn.execute(
        """SELECT f2.person_id AS pid, COUNT(DISTINCT f1.photo_id) AS n
           FROM faces f1 JOIN faces f2 ON f1.photo_id = f2.photo_id
           JOIN photos p ON p.id = f1.photo_id
           WHERE f1.person_id = ? AND f2.person_id IS NOT NULL AND f2.person_id != ? AND p.status='ok'
           GROUP BY f2.person_id ORDER BY n DESC LIMIT ?""",
        (person_id, person_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        p = conn.execute("SELECT id, name, display_no, cover_face_id FROM persons WHERE id=? AND merged_into IS NULL",
                         (r["pid"],)).fetchone()
        if p:
            out.append({"id": p["id"], "label": person_label(p), "cover_face_id": p["cover_face_id"],
                        "shared_photos": r["n"]})
    return out


def migrate_face_identities(conn: sqlite3.Connection, old_model: int, new_model: int,
                            iou_threshold: float = 0.55) -> dict:
    """Carry person assignments from one face model's detections to another's.

    A model upgrade re-detects every face, so the new rows have new ids and no
    identity. Boxes for the same physical face land in nearly the same place, so
    identities are transferred by box overlap within each photo. Embeddings are
    never mixed: the old rows keep their own model id and stop being used.
    """
    if old_model == new_model:
        return {"migrated": 0}
    old_rows = conn.execute(
        """SELECT photo_id, id, x1, y1, x2, y2, person_id, assign_source, assign_confidence
           FROM faces WHERE model_id = ? AND person_id IS NOT NULL""", (old_model,)).fetchall()
    if not old_rows:
        return {"migrated": 0}
    by_photo: dict[int, list] = defaultdict(list)
    for r in old_rows:
        by_photo[int(r["photo_id"])].append(r)

    migrated = locked = 0
    updates = []
    for photo_id, olds in by_photo.items():
        news = conn.execute(
            "SELECT id, x1, y1, x2, y2 FROM faces WHERE photo_id = ? AND model_id = ? AND person_id IS NULL",
            (photo_id, new_model)).fetchall()
        if not news:
            continue
        used: set[int] = set()
        for o in olds:
            best, best_iou = None, iou_threshold
            for n in news:
                if n["id"] in used:
                    continue
                iou = _box_iou((o["x1"], o["y1"], o["x2"], o["y2"]), (n["x1"], n["y1"], n["x2"], n["y2"]))
                if iou > best_iou:
                    best, best_iou = n, iou
            if best is None:
                continue
            used.add(best["id"])
            # A hand-confirmed identity stays hand-confirmed; an automatic one is
            # carried as a suggestion so re-clustering can still revise it.
            source = "user" if o["assign_source"] == "user" else "attach"
            if source == "user":
                locked += 1
            updates.append((o["person_id"], source, o["assign_confidence"] or best_iou, best["id"]))
            migrated += 1
    conn.executemany("UPDATE faces SET person_id=?, assign_source=?, assign_confidence=? WHERE id=?", updates)
    # Rejections follow the face they were made about.
    conn.commit()
    update_person_stats(conn)
    db.audit(conn, "faces_migrated", "model", new_model,
             {"from_model": old_model, "migrated": migrated, "user_confirmed": locked}, actor="system")
    db.bump_generation(conn, "people")
    conn.commit()
    out = {"migrated": migrated, "user_confirmed": locked, "photos": len(by_photo)}
    log.info("Face identity migration %s -> %s: %s", old_model, new_model, out)
    return out


def _box_iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def prune_stale_face_models(conn: sqlite3.Connection, keep_model: int) -> dict:
    """Delete embeddings from superseded face models once their identities are carried."""
    n = conn.execute("DELETE FROM faces WHERE model_id != ?", (keep_model,)).rowcount
    conn.commit()
    return {"deleted_faces": n}
