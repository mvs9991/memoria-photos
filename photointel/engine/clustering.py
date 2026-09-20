"""Face clustering: kNN graph -> Chinese Whispers -> cluster merge -> low-quality attach.

Design notes
------------
* Chinese Whispers (weighted label propagation) is used rather than connected
  components: a single bad edge cannot chain two identities together, because a
  node only adopts the label with the strongest *total* support.
* Only "clusterable" faces (sharp, large, frontal enough) form clusters; weak
  faces are attached afterwards at a stricter threshold so they cannot create or
  distort identities.
* Thresholds are calibrated on LFW (see eval/calibrate_faces.py) — not guessed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ..vectors import knn

log = logging.getLogger(__name__)


@dataclass
class ClusterParams:
    knn_k: int = 24
    edge_threshold: float = 0.42       # min cosine similarity for a graph edge
    # Avg top-3 linkage to merge two clusters. 0.55 sat on a percolation cliff: on
    # a real 56,617-face library it chained separate identities into one
    # 14,321-face "person" (mean intra-similarity 0.26, 69% of pairs below 0.3).
    # Swept against the share of faces landing in incoherent clusters (>15% of
    # intra-cluster pairs below 0.3): 5.6% at 0.64, 0.6% at 0.68, no further gain
    # above that but steadily more fragmentation. LFW clustering is unaffected
    # (BCubed F1 0.997 -> 0.996). Measured, not guessed — see README > Measured quality.
    merge_threshold: float = 0.68
    attach_threshold: float = 0.50     # min mean top-3 similarity to attach a weak face
    attach_margin: float = 0.04        # required lead over the runner-up identity
    min_cluster_size: int = 3
    min_quality: float = 0.35          # face quality to take part in clustering
    min_det_score: float = 0.60
    min_size_px: float = 40.0
    iterations: int = 12
    random_seed: int = 17


@dataclass
class ClusterResult:
    labels: np.ndarray                 # cluster id per face row (-1 = unassigned)
    n_clusters: int
    confidence: np.ndarray             # per-face assignment confidence
    cluster_confidence: dict = field(default_factory=dict)
    strong_mask: np.ndarray | None = None


def clusterable_mask(meta: dict, p: ClusterParams) -> np.ndarray:
    return ((meta["quality"] >= p.min_quality)
            & (meta["det_score"] >= p.min_det_score)
            & (meta["size_px"] >= p.min_size_px))


def chinese_whispers(n: int, src: np.ndarray, dst: np.ndarray, weight: np.ndarray,
                     iterations: int = 12, seed: int = 17) -> np.ndarray:
    """Weighted label propagation. Vectorised: each iteration is a sort + segmented reduction."""
    labels = np.arange(n, dtype=np.int64)
    if len(src) == 0:
        return labels
    rng = np.random.default_rng(seed)
    # symmetric edge list
    s = np.concatenate([src, dst])
    d = np.concatenate([dst, src])
    w = np.concatenate([weight, weight]).astype(np.float64)
    for it in range(iterations):
        nbr_labels = labels[d]
        order = np.lexsort((nbr_labels, s))
        ss, ll, ww = s[order], nbr_labels[order], w[order]
        # group boundaries for (node, label) pairs
        new_group = np.empty(len(ss), dtype=bool)
        new_group[0] = True
        np.not_equal(ss[1:], ss[:-1], out=new_group[1:])
        np.logical_or(new_group[1:], ll[1:] != ll[:-1], out=new_group[1:])
        starts = np.flatnonzero(new_group)
        sums = np.add.reduceat(ww, starts)
        g_node = ss[starts]
        g_label = ll[starts]
        # best label per node: sort groups by (node, -sum) and take the first of each node
        gorder = np.lexsort((-sums, g_node))
        gn, gl = g_node[gorder], g_label[gorder]
        first = np.empty(len(gn), dtype=bool)
        first[0] = True
        np.not_equal(gn[1:], gn[:-1], out=first[1:])
        best_nodes = gn[first]
        best_labels = gl[first]
        new_labels = labels.copy()
        if it < iterations - 1:
            # Update a random subset each round: breaks the oscillation that pure
            # synchronous updates cause on symmetric graphs.
            sel = rng.random(len(best_nodes)) < 0.75
            new_labels[best_nodes[sel]] = best_labels[sel]
        else:
            new_labels[best_nodes] = best_labels
        if np.array_equal(new_labels, labels):
            log.debug("Chinese Whispers converged after %d iterations", it + 1)
            labels = new_labels
            break
        labels = new_labels
    return labels


def _relabel(labels: np.ndarray, min_size: int) -> tuple[np.ndarray, int]:
    uniq, counts = np.unique(labels[labels >= 0], return_counts=True)
    keep = uniq[counts >= min_size]
    mapping = {int(u): i for i, u in enumerate(keep)}
    out = np.full(len(labels), -1, dtype=np.int64)
    for i, lab in enumerate(labels):
        if lab >= 0:
            m = mapping.get(int(lab))
            if m is not None:
                out[i] = m
    return out, len(keep)


def cluster_faces(mat: np.ndarray, meta: dict, params: ClusterParams | None = None,
                  device: str = "auto", cannot_link: set[tuple[int, int]] | None = None) -> ClusterResult:
    """Cluster face embeddings. `mat` must be L2-normalised (N, D)."""
    p = params or ClusterParams()
    n = len(mat)
    labels = np.full(n, -1, dtype=np.int64)
    conf = np.zeros(n, dtype=np.float32)
    if n == 0:
        return ClusterResult(labels, 0, conf)

    strong = clusterable_mask(meta, p)
    strong_idx = np.flatnonzero(strong)
    if len(strong_idx) == 0:
        log.warning("No faces pass the clustering quality bar (%d faces total)", n)
        return ClusterResult(labels, 0, conf, strong_mask=strong)

    sub = mat[strong_idx]
    k = min(p.knn_k, len(sub) - 1) if len(sub) > 1 else 0
    if k <= 0:
        labels[strong_idx] = np.arange(len(strong_idx))
        return ClusterResult(*_relabel(labels, p.min_cluster_size), conf, strong_mask=strong)

    nbr_idx, nbr_sim = knn(sub, sub, k, device=device, exclude_self=True)
    rows = np.repeat(np.arange(len(sub)), nbr_idx.shape[1])
    cols = nbr_idx.ravel()
    sims = nbr_sim.ravel()
    keep = sims >= p.edge_threshold
    src, dst, w = rows[keep], cols[keep], sims[keep]
    # Edge weight emphasises confident links (cubed similarity above threshold).
    w = ((w - p.edge_threshold) / max(1e-6, 1.0 - p.edge_threshold)) ** 2 + 1e-3

    sub_labels = chinese_whispers(len(sub), src, dst, w, iterations=p.iterations, seed=p.random_seed)
    sub_labels = _merge_clusters(sub, sub_labels, p, device=device)

    labels[strong_idx] = sub_labels
    labels, n_clusters = _relabel(labels, p.min_cluster_size)

    # Confidence for clustered faces: mean similarity to the 5 nearest same-cluster members.
    conf = _assignment_confidence(mat, labels, device=device)
    result = ClusterResult(labels=labels, n_clusters=n_clusters, confidence=conf, strong_mask=strong)
    result.cluster_confidence = _cluster_confidence(labels, conf, n_clusters)
    return result


def _merge_clusters(mat: np.ndarray, labels: np.ndarray, p: ClusterParams, device: str = "auto") -> np.ndarray:
    """Merge clusters whose members are mutually close (average of top-3 cross links)."""
    uniq = np.unique(labels)
    uniq = uniq[uniq >= 0]
    if len(uniq) < 2:
        return labels
    centroids = np.stack([_norm(mat[labels == u].mean(0)) for u in uniq])
    k = min(8, len(uniq) - 1)
    idx, sims = knn(centroids, centroids, k, device=device, exclude_self=True)
    parent = {int(u): int(u) for u in uniq}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    members = {int(u): np.flatnonzero(labels == u) for u in uniq}
    for i, u in enumerate(uniq):
        for j_pos in range(idx.shape[1]):
            v = uniq[idx[i, j_pos]]
            if sims[i, j_pos] < p.merge_threshold - 0.12:
                continue  # centroid gate: cheap pre-filter before the pairwise check
            a, b = members[int(u)], members[int(v)]
            if len(a) == 0 or len(b) == 0:
                continue
            cross = mat[a] @ mat[b].T
            top = np.sort(cross.ravel())[::-1][:3]
            if float(top.mean()) >= p.merge_threshold and float(np.median(cross)) >= p.merge_threshold - 0.18:
                ra, rb = find(int(u)), find(int(v))
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    out = labels.copy()
    for u in uniq:
        out[labels == u] = find(int(u))
    return out


def _norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _assignment_confidence(mat: np.ndarray, labels: np.ndarray, device: str = "auto") -> np.ndarray:
    conf = np.zeros(len(labels), dtype=np.float32)
    for lab in np.unique(labels[labels >= 0]):
        rows = np.flatnonzero(labels == lab)
        if len(rows) == 1:
            conf[rows] = 0.5
            continue
        sims = mat[rows] @ mat[rows].T
        np.fill_diagonal(sims, -1)
        k = min(5, len(rows) - 1)
        top = np.sort(sims, axis=1)[:, -k:]
        conf[rows] = np.clip(top.mean(axis=1), 0, 1)
    return conf


def _cluster_confidence(labels: np.ndarray, conf: np.ndarray, n_clusters: int) -> dict:
    out = {}
    for lab in range(n_clusters):
        rows = np.flatnonzero(labels == lab)
        if len(rows):
            out[lab] = float(np.clip(conf[rows].mean(), 0, 1))
    return out


def attach_faces(mat: np.ndarray, target_rows: np.ndarray, identity_rows: dict[int, np.ndarray],
                 p: ClusterParams, device: str = "auto", forbidden: dict[int, set[int]] | None = None,
                 ) -> dict[int, tuple[int, float]]:
    """Assign leftover faces to identities by mean top-3 similarity, with a margin test.

    identity_rows: identity id -> row indices of its current member faces.
    forbidden: face row -> identity ids the user rejected.
    Returns face row -> (identity id, confidence).
    """
    out: dict[int, tuple[int, float]] = {}
    if len(target_rows) == 0 or not identity_rows:
        return out
    ident_ids = list(identity_rows.keys())
    # Compare against up to 40 representative members per identity (keeps this O(N * 40 * P)).
    reps, owner = [], []
    rng = np.random.default_rng(p.random_seed)
    for iid in ident_ids:
        rows = identity_rows[iid]
        if len(rows) > 40:
            rows = rng.choice(rows, 40, replace=False)
        reps.append(mat[rows])
        owner.append(np.full(len(rows), iid, dtype=np.int64))
    ref = np.concatenate(reps)
    ref_owner = np.concatenate(owner)
    q = mat[target_rows]
    k = min(12, len(ref))
    idx, sims = knn(q, ref, k, device=device)
    for r in range(len(target_rows)):
        owners = ref_owner[idx[r]]
        scores: dict[int, list[float]] = {}
        for o, s in zip(owners, sims[r]):
            scores.setdefault(int(o), []).append(float(s))
        ranked = sorted(((np.mean(sorted(v, reverse=True)[:3]), iid) for iid, v in scores.items()), reverse=True)
        if not ranked:
            continue
        best_score, best_id = ranked[0]
        if forbidden and best_id in forbidden.get(int(target_rows[r]), set()):
            ranked = [x for x in ranked if x[1] not in forbidden.get(int(target_rows[r]), set())]
            if not ranked:
                continue
            best_score, best_id = ranked[0]
        runner = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score >= p.attach_threshold and (best_score - runner) >= p.attach_margin:
            out[int(target_rows[r])] = (int(best_id), float(best_score))
    return out


def suggest_merges(mat: np.ndarray, identity_rows: dict[int, np.ndarray], threshold: float = 0.50,
                   device: str = "auto", max_suggestions: int = 40) -> list[tuple[int, int, float]]:
    """Identity pairs that look like the same person (for user confirmation, never automatic)."""
    ids = [i for i, rows in identity_rows.items() if len(rows)]
    if len(ids) < 2:
        return []
    cents = np.stack([_norm(mat[identity_rows[i]].mean(0)) for i in ids])
    k = min(6, len(ids) - 1)
    idx, sims = knn(cents, cents, k, device=device, exclude_self=True)
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, float]] = []
    for i in range(len(ids)):
        for j_pos in range(idx.shape[1]):
            j = int(idx[i, j_pos])
            if sims[i, j_pos] < threshold - 0.1:
                continue
            a, b = ids[i], ids[j]
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            seen.add(key)
            ra, rb = identity_rows[a], identity_rows[b]
            cross = mat[ra] @ mat[rb].T
            score = float(np.sort(cross.ravel())[::-1][:3].mean())
            if score >= threshold:
                out.append((key[0], key[1], score))
    out.sort(key=lambda x: -x[2])
    return out[:max_suggestions]
