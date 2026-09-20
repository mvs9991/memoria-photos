"""People clustering and corrections, duplicates, events, locations."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from photointel import db
from photointel.engine import duplicates as dup_mod
from photointel.engine import events as events_mod
from photointel.engine import people as people_mod
from photointel.engine.clustering import ClusterParams, attach_faces, chinese_whispers, cluster_faces
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from tests.conftest import make_image


# ----------------------------------------------------------------- clustering primitives

def synthetic_faces(identities=5, per_identity=12, dim=128, intra_sim=0.62, seed=0):
    """Embeddings shaped like real ArcFace output.

    High-dimensional so unrelated vectors sit near cosine 0 (measured on LFW:
    0.013 +/- 0.04), with within-identity similarity around `intra_sim`
    (LFW mean was 0.70). The noise scale follows from cos ~ 1/(1 + s^2 * dim).
    """
    spread = float(np.sqrt((1.0 / intra_sim - 1.0) / dim))
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(identities, dim))
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    mat, labels = [], []
    for i, c in enumerate(centres):
        for _ in range(per_identity):
            v = c + rng.normal(scale=spread, size=dim)
            mat.append(v / np.linalg.norm(v))
            labels.append(i)
    mat = np.asarray(mat, dtype=np.float32)
    meta = {"quality": np.full(len(mat), 0.8, np.float32),
            "det_score": np.full(len(mat), 0.9, np.float32),
            "size_px": np.full(len(mat), 120.0, np.float32),
            "person_id": np.full(len(mat), -1, np.int64),
            "user_assigned": np.zeros(len(mat), bool)}
    return mat, np.array(labels), meta


def test_synthetic_geometry_matches_real_embeddings():
    """Guards the fixture itself: if this drifts, the clustering tests mean nothing."""
    mat, truth, _ = synthetic_faces()
    same = [float(mat[i] @ mat[j]) for i in range(len(mat)) for j in range(i + 1, len(mat))
            if truth[i] == truth[j]]
    diff = [float(mat[i] @ mat[j]) for i in range(len(mat)) for j in range(i + 1, len(mat))
            if truth[i] != truth[j]]
    assert 0.5 < np.mean(same) < 0.8
    assert abs(np.mean(diff)) < 0.08


def test_clustering_recovers_identities():
    mat, truth, meta = synthetic_faces()
    res = cluster_faces(mat, meta, ClusterParams(min_cluster_size=3), device="cpu")
    assert res.n_clusters == len(set(truth.tolist()))
    for lab in range(res.n_clusters):
        rows = np.flatnonzero(res.labels == lab)
        assert len(set(truth[rows].tolist())) == 1      # every cluster is pure


def test_clustering_leaves_singletons_unassigned():
    mat, truth, meta = synthetic_faces(identities=3, per_identity=10)
    rng = np.random.default_rng(5)
    strangers = rng.normal(size=(6, mat.shape[1])).astype(np.float32)
    strangers /= np.linalg.norm(strangers, axis=1, keepdims=True)
    mat2 = np.vstack([mat, strangers])
    meta2 = {k: np.concatenate([v, v[:6]]) for k, v in meta.items()}
    res = cluster_faces(mat2, meta2, ClusterParams(min_cluster_size=3), device="cpu")
    assert res.n_clusters == 3
    assert (res.labels[-6:] < 0).all()                  # one-off faces do not become people


def test_low_quality_faces_excluded_from_clustering():
    mat, truth, meta = synthetic_faces(identities=2, per_identity=8)
    meta["quality"][:4] = 0.05                          # blurry/tiny faces
    res = cluster_faces(mat, meta, ClusterParams(min_cluster_size=3), device="cpu")
    assert (res.labels[:4] < 0).all()


def test_chinese_whispers_finds_components():
    # two disconnected triangles
    src = np.array([0, 1, 2, 3, 4, 5])
    dst = np.array([1, 2, 0, 4, 5, 3])
    w = np.ones(6)
    labels = chinese_whispers(6, src, dst, w, iterations=8)
    assert len(set(labels[:3].tolist())) == 1
    assert len(set(labels[3:].tolist())) == 1
    assert labels[0] != labels[3]


def test_attach_respects_rejections():
    mat, truth, meta = synthetic_faces(identities=2, per_identity=8)
    identity_rows = {10: np.flatnonzero(truth == 0)[:6], 20: np.flatnonzero(truth == 1)[:6]}
    target = np.array([int(np.flatnonzero(truth == 0)[7])])
    p = ClusterParams()
    got = attach_faces(mat, target, identity_rows, p, device="cpu")
    assert got[int(target[0])][0] == 10
    blocked = attach_faces(mat, target, identity_rows, p, device="cpu",
                           forbidden={int(target[0]): {10}})
    assert int(target[0]) not in blocked or blocked[int(target[0])][0] != 10


# ----------------------------------------------------------------- people operations

@pytest.fixture
def indexed(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    people_mod.recluster(ctx, conn)
    return ctx, conn


def test_recluster_creates_people(indexed):
    ctx, conn = indexed
    n = conn.execute("SELECT COUNT(*) FROM persons WHERE face_count > 0").fetchone()[0]
    assert n >= 2


def test_rename_and_merge(indexed):
    ctx, conn = indexed
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM persons WHERE face_count > 0 ORDER BY face_count DESC LIMIT 2")]
    people_mod.rename_person(conn, ids[0], "Asha")
    assert conn.execute("SELECT name FROM persons WHERE id=?", (ids[0],)).fetchone()[0] == "Asha"
    total = sum(r[0] for r in conn.execute("SELECT face_count FROM persons WHERE id IN (?,?)", ids))
    people_mod.merge_persons(conn, ids[0], [ids[1]])
    merged = conn.execute("SELECT merged_into FROM persons WHERE id=?", (ids[1],)).fetchone()[0]
    assert merged == ids[0]
    assert conn.execute("SELECT face_count FROM persons WHERE id=?", (ids[0],)).fetchone()[0] == total
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='person_merged'").fetchone()[0] == 1


def test_split_creates_person_and_blocks_reassignment(indexed):
    ctx, conn = indexed
    pid = conn.execute("SELECT id FROM persons WHERE face_count >= 4 ORDER BY face_count DESC").fetchone()[0]
    faces = [r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=? LIMIT 2", (pid,))]
    out = people_mod.split_person(conn, pid, faces, name="Someone Else")
    new_id = out["created"]
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (faces[0],)).fetchone()[0] == new_id
    assert conn.execute("SELECT COUNT(*) FROM face_rejections WHERE face_id=? AND person_id=?",
                        (faces[0], pid)).fetchone()[0] == 1
    # re-clustering must not undo the user's decision
    people_mod.recluster(ctx, conn)
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (faces[0],)).fetchone()[0] == new_id


def test_user_assignment_survives_full_recluster(indexed):
    ctx, conn = indexed
    face_id, old_person = conn.execute(
        "SELECT id, person_id FROM faces WHERE person_id IS NOT NULL LIMIT 1").fetchone()
    target = people_mod.create_person(conn, "Manual Person", actor="user")
    people_mod.assign_faces(conn, [face_id], target)
    people_mod.recluster(ctx, conn, full=True)
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()[0] == target


def test_reject_removes_and_remembers(indexed):
    ctx, conn = indexed
    face_id, pid = conn.execute(
        "SELECT id, person_id FROM faces WHERE person_id IS NOT NULL LIMIT 1").fetchone()
    people_mod.reject_faces(conn, [face_id], pid)
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()[0] is None
    people_mod.recluster(ctx, conn)
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()[0] != pid


def test_person_stats_and_cover(indexed):
    ctx, conn = indexed
    row = conn.execute(
        "SELECT photo_count, face_count, cover_face_id, first_seen_ts FROM persons "
        "WHERE face_count > 0 ORDER BY face_count DESC LIMIT 1").fetchone()
    assert row["photo_count"] > 0 and row["cover_face_id"] is not None and row["first_seen_ts"]


# ----------------------------------------------------------------- duplicates

def test_exact_duplicates_grouped(indexed):
    ctx, conn = indexed
    dup_mod.find_duplicates(ctx, conn)
    row = conn.execute("SELECT id, kind, member_count, keep_photo_id FROM dup_groups WHERE kind='exact'").fetchone()
    assert row is not None and row["member_count"] == 2
    members = [r["photo_id"] for r in conn.execute(
        "SELECT photo_id FROM dup_members WHERE group_id=?", (row["id"],))]
    assert row["keep_photo_id"] in members
    # the original in DCIM should be preferred over the Backup copy
    keep_path = conn.execute("SELECT rel_path FROM photos WHERE id=?", (row["keep_photo_id"],)).fetchone()[0]
    assert keep_path.startswith("DCIM")


def test_duplicates_never_modify_photos(indexed):
    ctx, conn = indexed
    before = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM photos")}
    dup_mod.find_duplicates(ctx, conn)
    after = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM photos")}
    assert before == after


# ----------------------------------------------------------------- events

def test_events_split_by_time_gap(ctx, tmp_path):
    root = tmp_path / "evlib"
    base = datetime(2023, 4, 1, 9, 0)
    for i in range(6):
        make_image(root / "DCIM" / f"a{i}.jpg", colour=(30, 80, 150), taken=base + timedelta(minutes=8 * i))
    later = base + timedelta(days=3)
    for i in range(6):
        make_image(root / "DCIM" / f"b{i}.jpg", colour=(180, 90, 40), taken=later + timedelta(minutes=8 * i))
    Indexer(ctx, workers=2).run(roots=[str(root)])
    conn = ctx.connect()
    events_mod.detect_events(ctx, conn, events_mod.EventParams(min_photos=3))
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='event'").fetchone()[0] == 2
    # photos of one day all land in the same event
    rows = conn.execute("SELECT event_id, COUNT(*) n FROM photos WHERE event_id IS NOT NULL GROUP BY event_id")
    assert sorted(r["n"] for r in rows) == [6, 6]
    conn.close()


def test_small_clusters_are_not_events(ctx, tmp_path):
    root = tmp_path / "small"
    base = datetime(2023, 4, 1, 9, 0)
    for i in range(2):
        make_image(root / "DCIM" / f"s{i}.jpg", taken=base + timedelta(minutes=5 * i))
    Indexer(ctx, workers=2).run(roots=[str(root)])
    conn = ctx.connect()
    events_mod.detect_events(ctx, conn, events_mod.EventParams(min_photos=4))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    conn.close()


def test_screenshots_excluded_from_events(indexed):
    ctx, conn = indexed
    events_mod.detect_events(ctx, conn, events_mod.EventParams(min_photos=3))
    row = conn.execute(
        "SELECT event_id FROM photos WHERE source_kind='screenshot'").fetchone()
    assert row is None or row["event_id"] is None


def test_post_stages_run_end_to_end(indexed):
    ctx, conn = indexed
    out = run_post_stages(ctx, conn)
    assert "people" in out and "events" in out and "duplicates" in out
    for stage, result in out.items():
        assert "error" not in (result or {}), f"{stage} failed: {result}"


# ----------------------------------------------------------------- model upgrades

def test_identities_survive_a_face_model_upgrade(ctx, library, monkeypatch):
    """Re-analysing with a new face model must not lose the people you named."""
    from photointel.engine.people import migrate_face_identities

    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    people_mod.recluster(ctx, conn)
    pid = conn.execute("SELECT id FROM persons WHERE face_count > 0 ORDER BY face_count DESC").fetchone()[0]
    people_mod.rename_person(conn, pid, "Asha")
    faces = [r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=? LIMIT 3", (pid,))]
    people_mod.assign_faces(conn, faces, pid)          # hand-confirmed
    old_model = db.active_model_id(conn, "face")
    old_count = conn.execute("SELECT COUNT(*) FROM faces WHERE person_id=?", (pid,)).fetchone()[0]
    total_assigned = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL AND model_id=?", (old_model,)).fetchone()[0]

    # simulate a model upgrade: same boxes, new model id, no identities
    new_model = db.register_model(conn, "face", "next-gen-face", "2", 32)
    db.set_active_model(conn, "face", new_model)
    for r in conn.execute("SELECT photo_id, x1, y1, x2, y2, embedding, det_score, size_px, quality "
                          "FROM faces WHERE model_id=?", (old_model,)).fetchall():
        conn.execute("""INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality,
                        embedding, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                     (r["photo_id"], new_model, r["x1"], r["y1"], r["x2"], r["y2"], r["det_score"],
                      r["size_px"], r["quality"], r["embedding"]))
    conn.commit()

    out = migrate_face_identities(conn, old_model, new_model)
    assert out["migrated"] == total_assigned          # every assigned face carried, for every person
    carried = conn.execute("SELECT COUNT(*) FROM faces WHERE person_id=? AND model_id=?",
                           (pid, new_model)).fetchone()[0]
    assert carried == old_count
    assert conn.execute("SELECT name FROM persons WHERE id=?", (pid,)).fetchone()[0] == "Asha"
    # hand-confirmed faces stay hand-confirmed
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE model_id=? AND assign_source='user'",
                        (new_model,)).fetchone()[0] == len(faces)


def test_embeddings_from_different_models_are_never_mixed(ctx, library):
    from photointel.vectors import load_face_embeddings

    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    old_model = db.active_model_id(conn, "face")
    new_model = db.register_model(conn, "face", "other-face", "9", 32)
    conn.execute("""INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality,
                    embedding, created_at) SELECT photo_id, ?, x1, y1, x2, y2, det_score, size_px, quality,
                    embedding, 0 FROM faces WHERE model_id=?""", (new_model, old_model))
    conn.commit()
    ids_old, mat_old, _ = load_face_embeddings(conn, old_model)
    ids_new, mat_new, _ = load_face_embeddings(conn, new_model)
    assert len(ids_old) == len(ids_new) and not set(ids_old.tolist()) & set(ids_new.tolist())


def test_library_inspector_queries_are_valid(indexed, capsys, monkeypatch):
    """The no-ground-truth sanity report must actually run its SQL.

    Its queries are not exercised anywhere else, and a silently wrong one
    (trips counted against the wrong table) already shipped once.
    """
    import importlib.util
    import sys
    from pathlib import Path

    ctx, conn = indexed
    run_post_stages(ctx, conn)
    conn.commit()

    spec = importlib.util.spec_from_file_location(
        "inspect_library", Path(__file__).resolve().parent.parent / "eval" / "inspect_library.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.WARNINGS.clear()

    monkeypatch.setattr(sys, "argv", ["inspect_library", "--data", str(ctx.paths.data)])
    assert mod.main() == 0
    out = capsys.readouterr().out
    for section in ("FILES", "DATES", "PLACES", "PEOPLE", "EVENTS", "DUPLICATES"):
        assert f"=== {section} ===" in out


def test_clustering_does_not_chain_identities_through_bridges():
    """Faces that sit between two people must not fuse those people together.

    The merge step used to run at 0.55, which on a real 56,617-face library
    chained separate identities into one 14,321-face "person". Ordinary fixtures
    never show it: the failure needs borderline faces linking identity to
    identity, so this builds that chain explicitly. At 0.55 four clusters end up
    holding more than one true identity; at the shipped 0.64 none do.
    """
    n_ids = 30
    mat, truth, meta = synthetic_faces(identities=n_ids, per_identity=14, seed=7)
    rng = np.random.default_rng(11)
    centres = [mat[truth == i].mean(0) for i in range(n_ids)]
    bridges = []
    for a in range(n_ids - 1):                       # a path touching every identity
        ca, cb = centres[a], centres[a + 1]
        for t in np.linspace(0.35, 0.65, 10):
            v = (1 - t) * ca + t * cb + rng.normal(scale=0.04, size=mat.shape[1])
            bridges.append(v / np.linalg.norm(v))
    mat2 = np.vstack([mat, np.asarray(bridges, dtype=np.float32)])
    truth2 = np.concatenate([truth, np.full(len(bridges), -1)])
    meta2 = {k: np.concatenate([v, np.repeat(v[:1], len(bridges), axis=0)]) for k, v in meta.items()}

    res = cluster_faces(mat2, meta2, ClusterParams(min_cluster_size=3), device="cpu")

    mixed = 0
    for lab in set(res.labels[res.labels >= 0].tolist()):
        ids = set(truth2[(res.labels == lab) & (truth2 >= 0)].tolist())
        if len(ids) > 1:
            mixed += 1
    assert mixed == 0, f"{mixed} clusters hold more than one identity — the merge step is chaining"
    assert res.n_clusters >= n_ids, "identities were lost rather than kept apart"


@pytest.mark.parametrize("leaf,expected", [
    ("Photos from 2011", None),        # Google Takeout's generic year folder
    ("From", None),                    # nothing but a preposition
    ("Goa Trip 2019", "GOA Trip"),     # a real name survives (<=3 letters read as a code)
    ("DCIM", None),
    ("Photos from Goa", "GOA"),        # the connective goes, the place stays
    ("Jhani Marriage", "Jhani Marriage"),
])
def test_folder_hint_rejects_meaningless_names(leaf, expected):
    """A folder hint must never become a title made only of connectives."""
    from collections import Counter
    from types import SimpleNamespace

    from photointel.engine.events import _folder_hint

    seg = SimpleNamespace(folders=Counter({f"/library/{leaf}": 10}), photo_ids=list(range(10)))
    assert _folder_hint(seg) == expected
