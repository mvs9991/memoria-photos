"""Calibrate face verification / clustering thresholds on LFW.

Reports:
  * verification: TAR at fixed FAR over all same/different pairs
  * clustering: BCubed precision / recall / F1 and identity counts

Run `eval/embed_lfw.py` first. Numbers printed here are measurements on LFW
(frontal-ish celebrity photos) — a personal library is usually harder, so treat
them as an upper bound, not a promise.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photointel.engine.clustering import ClusterParams, cluster_faces  # noqa: E402
from photointel.vectors import normalize  # noqa: E402

DATASETS = Path(os.environ.get("PI_DATASETS", "D:/pi_cache/datasets"))
NPZ = DATASETS / "lfw_embeddings.npz"


def load(min_images: int = 1, max_identities: int | None = None, seed: int = 0):
    d = np.load(NPZ, allow_pickle=True)
    emb = normalize(d["emb"].astype(np.float32))
    label = d["label"]
    meta = {"quality": d["quality"], "det_score": d["det_score"], "size_px": d["size_px"],
            "person_id": np.full(len(label), -1, dtype=np.int64),
            "user_assigned": np.zeros(len(label), dtype=bool)}
    counts = Counter(label.tolist())
    keep_labels = {l for l, c in counts.items() if c >= min_images}
    if max_identities:
        rng = np.random.default_rng(seed)
        keep_labels = set(rng.choice(sorted(keep_labels), min(max_identities, len(keep_labels)), replace=False).tolist())
    mask = np.array([l in keep_labels for l in label])
    return emb[mask], label[mask], {k: v[mask] for k, v in meta.items()}


def verification_metrics(emb: np.ndarray, label: np.ndarray, sample_pairs: int = 400_000, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    n = len(emb)
    idx_by_label: dict[int, list[int]] = defaultdict(list)
    for i, l in enumerate(label):
        idx_by_label[int(l)].append(i)
    same_pairs = []
    for rows in idx_by_label.values():
        if len(rows) < 2:
            continue
        for _ in range(min(len(rows) * 2, 60)):
            a, b = rng.choice(rows, 2, replace=False)
            same_pairs.append((a, b))
    same_pairs = same_pairs[:sample_pairs]
    a = rng.integers(0, n, sample_pairs)
    b = rng.integers(0, n, sample_pairs)
    diff_mask = label[a] != label[b]
    diff_sims = (emb[a[diff_mask]] * emb[b[diff_mask]]).sum(1)
    same_sims = np.array([float(emb[x] @ emb[y]) for x, y in same_pairs])
    out = {"same_pairs": len(same_sims), "diff_pairs": len(diff_sims),
           "same_mean": float(same_sims.mean()), "diff_mean": float(diff_sims.mean())}
    for far in (1e-2, 1e-3, 1e-4, 1e-5):
        thr = float(np.quantile(diff_sims, 1 - far))
        out[f"TAR@FAR={far:g}"] = round(float((same_sims >= thr).mean()), 4)
        out[f"thr@FAR={far:g}"] = round(thr, 4)
    # best-accuracy threshold
    grid = np.arange(0.20, 0.70, 0.01)
    accs = [( ((same_sims >= t).mean() + (diff_sims < t).mean()) / 2, t) for t in grid]
    acc, thr = max(accs)
    out["best_acc"] = round(float(acc), 4)
    out["best_acc_threshold"] = round(float(thr), 3)
    return out


def bcubed(labels_true: np.ndarray, labels_pred: np.ndarray) -> tuple[float, float, float]:
    """BCubed precision/recall/F1. Unassigned (-1) predictions count as singletons."""
    pred = labels_pred.copy()
    next_id = int(pred.max()) + 1 if len(pred) else 0
    for i in range(len(pred)):
        if pred[i] < 0:
            pred[i] = next_id
            next_id += 1
    true_idx: dict[int, list[int]] = defaultdict(list)
    pred_idx: dict[int, list[int]] = defaultdict(list)
    for i, (t, p) in enumerate(zip(labels_true, pred)):
        true_idx[int(t)].append(i)
        pred_idx[int(p)].append(i)
    precision = recall = 0.0
    n = len(labels_true)
    true_of = {i: int(t) for i, t in enumerate(labels_true)}
    pred_of = {i: int(p) for i, p in enumerate(pred)}
    for i in range(n):
        same_pred = pred_idx[pred_of[i]]
        same_true = true_idx[true_of[i]]
        correct = sum(1 for j in same_pred if true_of[j] == true_of[i])
        precision += correct / len(same_pred)
        recall += correct / len(same_true)
    precision /= n
    recall /= n
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def pairwise_metrics(labels_true: np.ndarray, labels_pred: np.ndarray) -> tuple[float, float]:
    """Pairwise precision/recall over predicted clusters (ignores -1)."""
    tp = fp = fn = 0
    by_pred: dict[int, list[int]] = defaultdict(list)
    for i, p in enumerate(labels_pred):
        if p >= 0:
            by_pred[int(p)].append(i)
    by_true: dict[int, list[int]] = defaultdict(list)
    for i, t in enumerate(labels_true):
        by_true[int(t)].append(i)
    for rows in by_pred.values():
        c = Counter(int(labels_true[i]) for i in rows)
        total = len(rows)
        tp += sum(v * (v - 1) // 2 for v in c.values())
        fp += total * (total - 1) // 2 - sum(v * (v - 1) // 2 for v in c.values())
    for rows in by_true.values():
        c = Counter(int(labels_pred[i]) for i in rows)
        same = sum(v * (v - 1) // 2 for v in c.values() if v > 1)
        fn += len(rows) * (len(rows) - 1) // 2 - same
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identities", type=int, default=600, help="sample size for the clustering sweep")
    ap.add_argument("--min-images", type=int, default=1)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--recurring", type=int, default=60, help="identities with many photos")
    ap.add_argument("--min-recurring", type=int, default=12)
    ap.add_argument("--max-per-person", type=int, default=40)
    ap.add_argument("--singletons", type=int, default=800)
    ap.add_argument("--min-cluster-size", type=int, default=3)
    args = ap.parse_args()

    emb, label, meta = load()
    print(f"LFW: {len(emb)} faces, {len(set(label.tolist()))} identities")
    print("\n== verification ==")
    v = verification_metrics(emb, label)
    for k, val in v.items():
        print(f"  {k:22s} {val}")

    # ---- realistic scenario: recurring people + one-off strangers ------------------------
    d = np.load(NPZ, allow_pickle=True)
    emb_all = normalize(d["emb"].astype(np.float32))
    lab_all = d["label"]
    qual_all, det_all, size_all = d["quality"], d["det_score"], d["size_px"]
    counts = Counter(lab_all.tolist())
    rng = np.random.default_rng(3)
    recurring = [l for l, c in counts.items() if c >= args.min_recurring]
    singles = [l for l, c in counts.items() if c == 1]
    rng.shuffle(recurring)
    recurring = recurring[: args.recurring]
    singles = list(rng.choice(singles, min(args.singletons, len(singles)), replace=False))
    rows = []
    for l in recurring:
        idx = np.flatnonzero(lab_all == l)
        if len(idx) > args.max_per_person:
            idx = rng.choice(idx, args.max_per_person, replace=False)
        rows.extend(idx.tolist())
    for l in singles:
        rows.extend(np.flatnonzero(lab_all == l).tolist())
    rows = np.array(sorted(rows))
    emb2, label2 = emb_all[rows], lab_all[rows]
    meta2 = {"quality": qual_all[rows], "det_score": det_all[rows], "size_px": size_all[rows],
             "person_id": np.full(len(rows), -1, dtype=np.int64), "user_assigned": np.zeros(len(rows), dtype=bool)}
    n_true = len(set(label2.tolist()))
    print(f"\n== clustering scenario: {len(emb2)} faces, {len(recurring)} recurring people "
          f"(>={args.min_recurring} photos) + {len(singles)} strangers, {n_true} identities ==")
    print(f"{'edge':>5} {'merge':>6} {'minsz':>6} {'clusters':>9} {'BC-P':>6} {'BC-R':>6} {'BC-F1':>6} "
          f"{'pair-P':>7} {'pair-R':>7} {'split':>6} {'mixed':>6} {'unasgn':>7} {'sec':>5}")
    edges = [0.42] if not args.quick else [0.42]
    merges = [0.55, 0.64] if not args.quick else [0.55]
    best = None
    for edge in edges:
        for merge in merges:
            p = ClusterParams(edge_threshold=edge, merge_threshold=merge,
                              min_cluster_size=args.min_cluster_size,
                              min_quality=0.0, min_det_score=0.0, min_size_px=0.0)
            t0 = time.time()
            res = cluster_faces(emb2, meta2, p, device="cuda")
            el = time.time() - t0
            bp, br, bf = bcubed(label2, res.labels)
            pp, pr = pairwise_metrics(label2, res.labels)
            # how many recurring identities got split across clusters / clusters mixing identities
            split = 0
            for l in recurring:
                sel = res.labels[label2 == l]
                sel = sel[sel >= 0]
                if len(set(sel.tolist())) > 1:
                    split += 1
            mixed = 0
            for c in set(res.labels[res.labels >= 0].tolist()):
                if len(set(label2[res.labels == c].tolist())) > 1:
                    mixed += 1
            unassigned = float((res.labels < 0).mean() * 100)
            print(f"{edge:5.2f} {merge:6.2f} {p.min_cluster_size:6d} {res.n_clusters:9d} "
                  f"{bp:6.3f} {br:6.3f} {bf:6.3f} {pp:7.3f} {pr:7.3f} {split:6d} {mixed:6d} "
                  f"{unassigned:6.1f}% {el:5.1f}")
            if best is None or bf > best[0]:
                best = (bf, edge, merge)
    print(f"\nbest BCubed F1 {best[0]:.3f} at edge={best[1]} merge={best[2]}")


if __name__ == "__main__":
    main()
