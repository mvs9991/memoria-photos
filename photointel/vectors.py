"""Vector storage and similarity search.

Embeddings live in SQLite as float16 blobs and are loaded into a contiguous
float32 matrix for search. Exact (brute-force) search is used deliberately: it is
~10 ms for 100k x 768 on the GPU, has no index-build step, and never returns the
approximate-recall surprises that hurt duplicate/face recall.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


def _torch():
    try:
        import torch

        return torch
    except Exception:  # pragma: no cover
        return None


def load_photo_embeddings(conn: sqlite3.Connection, model_id: int, only_active: bool = True
                          ) -> tuple[np.ndarray, np.ndarray]:
    sql = ("SELECT e.photo_id, e.vec FROM photo_embeddings e "
           "JOIN photos p ON p.id = e.photo_id WHERE e.model_id = ?")
    if only_active:
        sql += " AND p.status = 'ok'"
    sql += " ORDER BY e.photo_id"
    rows = conn.execute(sql, (model_id,)).fetchall()
    if not rows:
        return np.zeros((0,), np.int64), np.zeros((0, 0), np.float32)
    ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    dim = len(rows[0][1]) // 2
    mat = np.empty((len(rows), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        mat[i] = np.frombuffer(r[1], dtype=np.float16, count=dim)
    return ids, mat


def load_face_embeddings(conn: sqlite3.Connection, model_id: int) -> tuple[np.ndarray, np.ndarray, dict]:
    rows = conn.execute(
        "SELECT f.id, f.photo_id, f.embedding, f.quality, f.det_score, f.size_px, f.person_id, f.assign_source "
        "FROM faces f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.model_id = ? AND p.status != 'missing' ORDER BY f.id", (model_id,)
    ).fetchall()
    if not rows:
        return np.zeros((0,), np.int64), np.zeros((0, 0), np.float32), {}
    n = len(rows)
    dim = len(rows[0][2]) // 2
    ids = np.empty(n, dtype=np.int64)
    mat = np.empty((n, dim), dtype=np.float32)
    meta = {
        "photo_id": np.empty(n, dtype=np.int64),
        "quality": np.empty(n, dtype=np.float32),
        "det_score": np.empty(n, dtype=np.float32),
        "size_px": np.empty(n, dtype=np.float32),
        "person_id": np.full(n, -1, dtype=np.int64),
        "user_assigned": np.zeros(n, dtype=bool),
    }
    for i, r in enumerate(rows):
        ids[i] = r[0]
        mat[i] = np.frombuffer(r[2], dtype=np.float16, count=dim)
        meta["photo_id"][i] = r[1]
        meta["quality"][i] = r[3] if r[3] is not None else 0.0
        meta["det_score"][i] = r[4] if r[4] is not None else 0.0
        meta["size_px"][i] = r[5] if r[5] is not None else 0.0
        if r[6] is not None:
            meta["person_id"][i] = r[6]
        meta["user_assigned"][i] = (r[7] == "user")
    return ids, mat, meta


def normalize(mat: np.ndarray) -> np.ndarray:
    if mat.size == 0:
        return mat
    n = np.linalg.norm(mat, axis=1, keepdims=True)
    np.maximum(n, 1e-8, out=n)
    return mat / n


# Similarity elements per chunk. 100M fp16 = 200 MB per block, which leaves room
# for the models on a 4 GB card and is still one big efficient matmul.
CHUNK_ELEMENTS = 100_000_000


def knn(queries: np.ndarray, refs: np.ndarray, k: int, device: str = "auto", chunk: int | None = None,
        exclude_self: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Exact top-k cosine similarity (inputs must be L2-normalised).

    Returns (indices [Q,k], similarities [Q,k]). Uses the GPU when available. The
    chunk size adapts to |refs| so peak memory stays bounded no matter how large
    the library gets — a fixed chunk would allocate gigabytes at 150k+ vectors.
    """
    if queries.size == 0 or refs.size == 0:
        return np.zeros((len(queries), 0), np.int64), np.zeros((len(queries), 0), np.float32)
    if chunk is None:
        chunk = int(max(64, min(4096, CHUNK_ELEMENTS // max(len(refs), 1))))
    k_eff = min(k + (1 if exclude_self else 0), len(refs))
    torch = _torch()
    use_cuda = torch is not None and device != "cpu" and torch.cuda.is_available()
    idx_out = np.empty((len(queries), k_eff), dtype=np.int64)
    sim_out = np.empty((len(queries), k_eff), dtype=np.float32)
    if torch is not None:
        dev = "cuda" if use_cuda else "cpu"
        dtype = torch.float16 if use_cuda else torch.float32
        # 4 GB GPU: keep the reference matrix resident if it fits, else stream it.
        ref_bytes = refs.size * (2 if use_cuda else 4)
        stream_refs = use_cuda and ref_bytes > 1_200_000_000
        if not stream_refs:
            ref_t = torch.from_numpy(refs).to(dev, dtype=dtype)
            for start in range(0, len(queries), chunk):
                q = torch.from_numpy(queries[start:start + chunk]).to(dev, dtype=dtype)
                sims = q @ ref_t.T
                s, i = torch.topk(sims, k_eff, dim=1)   # topk on fp16: no full-size float copy
                idx_out[start:start + chunk] = i.cpu().numpy()
                sim_out[start:start + chunk] = s.float().cpu().numpy()
                del sims, s, i
            del ref_t
        else:  # pragma: no cover - only for very large libraries
            for start in range(0, len(queries), chunk):
                q = torch.from_numpy(queries[start:start + chunk]).to(dev, dtype=dtype)
                best_s, best_i = None, None
                for rs in range(0, len(refs), 200_000):
                    ref_t = torch.from_numpy(refs[rs:rs + 200_000]).to(dev, dtype=dtype)
                    sims = (q @ ref_t.T).float()
                    s, i = torch.topk(sims, min(k_eff, sims.shape[1]), dim=1)
                    i = i + rs
                    if best_s is None:
                        best_s, best_i = s, i
                    else:
                        cs = torch.cat([best_s, s], 1)
                        ci = torch.cat([best_i, i], 1)
                        s2, sel = torch.topk(cs, k_eff, dim=1)
                        best_s, best_i = s2, torch.gather(ci, 1, sel)
                    del ref_t, sims
                idx_out[start:start + chunk] = best_i.cpu().numpy()
                sim_out[start:start + chunk] = best_s.cpu().numpy()
        if use_cuda:
            torch.cuda.empty_cache()
    else:  # numpy fallback
        for start in range(0, len(queries), chunk):
            sims = queries[start:start + chunk] @ refs.T
            i = np.argpartition(-sims, k_eff - 1, axis=1)[:, :k_eff]
            s = np.take_along_axis(sims, i, axis=1)
            order = np.argsort(-s, axis=1)
            idx_out[start:start + chunk] = np.take_along_axis(i, order, axis=1)
            sim_out[start:start + chunk] = np.take_along_axis(s, order, axis=1)
    if exclude_self:
        # Drop exactly one column per row: the self-match, or the weakest neighbour
        # when self is absent (identical vectors can push it out of the top-k).
        # Vectorised — a Python loop here costs seconds at 100k+ queries.
        n = len(queries)
        rows = np.arange(n)[:, None]
        mask = idx_out == rows
        has_self = mask.any(axis=1)
        drop_col = np.where(has_self, mask.argmax(axis=1), k_eff - 1)
        keep = np.ones((n, k_eff), dtype=bool)
        keep[np.arange(n), drop_col] = False
        return (idx_out[keep].reshape(n, k_eff - 1), sim_out[keep].reshape(n, k_eff - 1))
    return idx_out, sim_out


@dataclass
class PhotoIndex:
    ids: np.ndarray
    mat: np.ndarray
    model_id: int
    generation: int
    loaded_at: float

    def id_to_row(self) -> dict[int, int]:
        if not hasattr(self, "_map"):
            self._map = {int(pid): i for i, pid in enumerate(self.ids)}
        return self._map

    def search(self, query: np.ndarray, k: int = 200, subset: np.ndarray | None = None,
               device: str = "auto") -> tuple[np.ndarray, np.ndarray]:
        """Returns (photo_ids, similarities) sorted by similarity."""
        if len(self.ids) == 0:
            return np.zeros(0, np.int64), np.zeros(0, np.float32)
        q = query.reshape(1, -1).astype(np.float32)
        q = normalize(q)
        if subset is not None and len(subset) < len(self.ids):
            m = self.id_to_row()
            rows = np.array([m[int(i)] for i in subset if int(i) in m], dtype=np.int64)
            if rows.size == 0:
                return np.zeros(0, np.int64), np.zeros(0, np.float32)
            sims = (self.mat[rows] @ q[0]).astype(np.float32)
            k_eff = min(k, len(rows))
            top = np.argpartition(-sims, k_eff - 1)[:k_eff]
            top = top[np.argsort(-sims[top])]
            return self.ids[rows[top]], sims[top]
        idx, sims = knn(q, self.mat, min(k, len(self.ids)), device=device)
        return self.ids[idx[0]], sims[0]


class IndexCache:
    """Caches the photo embedding matrix for the API, refreshed when generation changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._index: PhotoIndex | None = None

    def get(self, conn: sqlite3.Connection, model_id: int, generation: int) -> PhotoIndex:
        with self._lock:
            idx = self._index
            if idx is None or idx.model_id != model_id or idx.generation != generation:
                t0 = time.time()
                ids, mat = load_photo_embeddings(conn, model_id)
                mat = normalize(mat)
                idx = PhotoIndex(ids=ids, mat=mat, model_id=model_id, generation=generation, loaded_at=time.time())
                self._index = idx
                log.info("Loaded %d photo embeddings (dim %d) in %.2fs", len(ids),
                         mat.shape[1] if mat.size else 0, time.time() - t0)
            return idx

    def invalidate(self) -> None:
        with self._lock:
            self._index = None
