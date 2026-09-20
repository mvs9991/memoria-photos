"""Streaming indexing pipeline.

    feeder ──► [task queue] ──► N CPU workers ──► [gpu queue] ──► GPU stage ──► [write queue] ──► DB writer
               (bounded)        read/hash/decode/  (bounded)       faces +          (bounded)       batched
                                EXIF/thumb/quality                 embeddings                       transactions

* Every per-photo failure is isolated, logged to `processing_errors`, and the run continues.
* Progress is committed in small transactions; an interrupted run resumes where it left off
  because each photo records which analysis versions/models have been applied.
* Originals are opened read-only; only thumbnails are written (to the cache directory).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .. import db, hashing, imaging, metadata, quality
from ..context import AppContext

log = logging.getLogger(__name__)

META_VERSION = 1
WORK_SIZE = 1600          # long side of the decoded working image
IN_MEMORY_READ_LIMIT = 96 << 20
_SENTINEL = object()


@dataclass
class Task:
    photo_id: int
    abs_path: str
    rel_path: str
    folder: str
    filename: str
    ext: str
    size: int
    mtime: float
    ctime: float | None
    sha256: str | None
    need_meta: bool
    need_faces: bool
    need_semantic: bool


@dataclass
class Result:
    task: Task
    meta: dict | None = None
    error: str | None = None
    error_stage: str | None = None
    work: np.ndarray | None = None
    orig_size: tuple[int, int] = (0, 0)
    sem_input: np.ndarray | None = None
    det_input: tuple | None = None
    faces: list | None = None
    embedding: np.ndarray | None = None
    timings: dict = field(default_factory=dict)


class CancelledError(Exception):
    pass


class Indexer:
    def __init__(self, ctx: AppContext, job_id: int | None = None,
                 progress_cb: Callable[[str, int, int, str], None] | None = None,
                 enable_faces: bool = True, enable_semantic: bool = True, workers: int | None = None):
        self.ctx = ctx
        self.job_id = job_id
        self.progress_cb = progress_cb
        self.enable_faces = enable_faces
        self.enable_semantic = enable_semantic
        n = workers or ctx.settings.workers or max(2, min(8, (os.cpu_count() or 4)))
        self.n_workers = n
        self.stop_event = threading.Event()
        self.stats = {"processed": 0, "errors": 0, "faces": 0, "moved": 0, "skipped": 0}
        self._stage_times: dict[str, float] = {}
        self._fatal: BaseException | None = None

    # ------------------------------------------------------------------ progress / cancel
    def _report(self, stage: str, done: int, total: int, message: str = "") -> None:
        if self.progress_cb:
            try:
                self.progress_cb(stage, done, total, message)
            except Exception:  # progress must never break indexing
                log.debug("progress callback failed", exc_info=True)

    def cancel(self) -> None:
        self.stop_event.set()

    def _check_cancel(self, conn) -> bool:
        if self.stop_event.is_set():
            return True
        if self.job_id is not None:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (self.job_id,)).fetchone()
            if row and row[0]:
                self.stop_event.set()
                return True
        return False

    # ------------------------------------------------------------------ public API
    def scan(self, roots: list[str]) -> list:
        from .scanner import ensure_root, scan_root

        conn = self.ctx.connect()
        try:
            results = []
            for r in roots:
                root_id = ensure_root(conn, r)
                self._report("scan", 0, 0, f"Scanning {r}")
                stats = scan_root(conn, root_id, Path(r), exclude=[self.ctx.paths.data],
                                  progress=lambda n: self._report("scan", n, 0, f"Found {n:,} files"),
                                  should_stop=lambda: self._check_cancel(conn))
                results.append(stats)
            return results
        finally:
            conn.close()

    def pending_tasks(self, conn, retry_errors: bool = False) -> list[Task]:
        face_mid = self.ctx.face_model_id(conn) if self.enable_faces else None
        sem_mid = self.ctx.semantic_model_id(conn) if self.enable_semantic else None
        rows = conn.execute(
            "SELECT p.id, r.path AS root, p.rel_path, p.folder, p.filename, p.ext, p.size, p.mtime, p.ctime, "
            "p.sha256, p.meta_version, p.faces_model, p.semantic_model, p.status "
            "FROM photos p JOIN roots r ON r.id = p.root_id WHERE p.status != 'missing' ORDER BY p.id"
        ).fetchall()
        tasks = []
        for r in rows:
            if r["status"] == "error" and not retry_errors and r["meta_version"] is not None:
                continue
            need_meta = r["meta_version"] is None or r["meta_version"] < META_VERSION or (r["status"] == "error" and retry_errors)
            need_faces = face_mid is not None and r["faces_model"] != face_mid
            need_sem = sem_mid is not None and r["semantic_model"] != sem_mid
            if not (need_meta or need_faces or need_sem):
                continue
            tasks.append(Task(
                photo_id=r["id"], abs_path=os.path.join(r["root"], r["rel_path"]), rel_path=r["rel_path"],
                folder=r["folder"], filename=r["filename"], ext=r["ext"], size=r["size"], mtime=r["mtime"],
                ctime=r["ctime"], sha256=r["sha256"], need_meta=need_meta, need_faces=need_faces, need_semantic=need_sem,
            ))
        return tasks

    def run(self, roots: list[str] | None = None, retry_errors: bool = False) -> dict:
        t_start = time.time()
        if roots:
            self.scan(roots)
        conn = self.ctx.connect()
        try:
            tasks = self.pending_tasks(conn, retry_errors=retry_errors)
            self.face_mid = self.ctx.face_model_id(conn) if self.enable_faces else None
            self.sem_mid = self.ctx.semantic_model_id(conn) if self.enable_semantic else None
        finally:
            conn.close()
        total = len(tasks)
        # A face-model change re-detects everything; remember the previous model so
        # people can be carried over afterwards instead of silently disappearing.
        self.previous_face_model = None
        if self.face_mid is not None:
            conn = self.ctx.connect()
            try:
                row = conn.execute(
                    "SELECT faces_model, COUNT(*) n FROM photos WHERE faces_model IS NOT NULL "
                    "AND faces_model != ? GROUP BY faces_model ORDER BY n DESC LIMIT 1",
                    (self.face_mid,)).fetchone()
                if row and row["n"] > 0:
                    self.previous_face_model = int(row["faces_model"])
                    log.info("Face model changed (%s -> %s): identities will be carried over by box overlap",
                             self.previous_face_model, self.face_mid)
            finally:
                conn.close()
        log.info("Analysis queue: %d photos (workers=%d, faces=%s, semantic=%s)", total, self.n_workers,
                 self.enable_faces, self.enable_semantic)
        if total:
            self._run_pipeline(tasks)
        if getattr(self, "previous_face_model", None) and not self.stop_event.is_set():
            from ..engine.people import migrate_face_identities

            conn = self.ctx.connect()
            try:
                self.stats["identity_migration"] = migrate_face_identities(
                    conn, self.previous_face_model, self.face_mid)
            finally:
                conn.close()
        self.stats["seconds"] = round(time.time() - t_start, 1)
        self.stats["total"] = total
        if self._fatal is not None:
            raise self._fatal
        if self.stop_event.is_set():
            raise CancelledError("Indexing cancelled")
        return self.stats

    # ------------------------------------------------------------------ pipeline
    def _run_pipeline(self, tasks: list[Task]) -> None:
        need_gpu = any(t.need_faces or t.need_semantic for t in tasks)
        face_engine = self.ctx.face_engine() if (self.enable_faces and any(t.need_faces for t in tasks)) else None
        sem_model = self.ctx.semantic_model() if (self.enable_semantic and any(t.need_semantic for t in tasks)) else None
        if sem_model is not None:
            conn = self.ctx.connect()
            self.sem_mid = self.ctx.semantic_model_id(conn)
            conn.close()
        self.face_engine, self.sem_model = face_engine, sem_model

        task_q: queue.Queue = queue.Queue(maxsize=self.n_workers * 4)
        gpu_q: queue.Queue = queue.Queue(maxsize=32)
        write_q: queue.Queue = queue.Queue(maxsize=512)
        total = len(tasks)

        def feeder():
            for t in tasks:
                while not self.stop_event.is_set():
                    try:
                        task_q.put(t, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                if self.stop_event.is_set():
                    break
            for _ in range(self.n_workers):
                task_q.put(_SENTINEL)

        def cpu_worker():
            try:
                while True:
                    t = task_q.get()
                    if t is _SENTINEL:
                        break
                    if self.stop_event.is_set():
                        continue
                    res = self._cpu_stage(t)
                    target = gpu_q if (need_gpu and res.error is None and (res.work is not None or res.sem_input is not None)) else write_q
                    self._put(target, res)
            except BaseException as exc:  # pragma: no cover - defensive
                self._fatal = exc
                self.stop_event.set()

        workers = [threading.Thread(target=cpu_worker, name=f"cpu-{i}", daemon=True) for i in range(self.n_workers)]
        feeder_t = threading.Thread(target=feeder, name="feeder", daemon=True)
        gpu_t = threading.Thread(target=self._gpu_loop, args=(gpu_q, write_q), name="gpu", daemon=True)
        writer_t = threading.Thread(target=self._writer_loop, args=(write_q, total), name="writer", daemon=True)

        feeder_t.start()
        for w in workers:
            w.start()
        gpu_t.start()
        writer_t.start()
        try:
            for w in workers:
                while w.is_alive():
                    w.join(timeout=0.5)
            self._put(gpu_q, _SENTINEL, force=True)
            while gpu_t.is_alive():
                gpu_t.join(timeout=0.5)
            self._put(write_q, _SENTINEL, force=True)
            while writer_t.is_alive():
                writer_t.join(timeout=0.5)
        except KeyboardInterrupt:
            log.warning("Interrupted; finishing in-flight writes")
            self.stop_event.set()
            self._put(gpu_q, _SENTINEL, force=True)
            gpu_t.join(timeout=30)
            self._put(write_q, _SENTINEL, force=True)
            writer_t.join(timeout=60)
            raise
        log.info("Stage timings (s, summed over threads): %s",
                 {k: round(v, 1) for k, v in sorted(self._stage_times.items())})

    def _put(self, q: queue.Queue, item, force: bool = False) -> None:
        while True:
            try:
                q.put(item, timeout=0.5)
                return
            except queue.Full:
                if self.stop_event.is_set() and not force:
                    return

    def _timed(self, key: str, t0: float) -> float:
        now = time.perf_counter()
        self._stage_times[key] = self._stage_times.get(key, 0.0) + (now - t0)
        return now

    # ------------------------------------------------------------------ CPU stage
    def _cpu_stage(self, t: Task) -> Result:
        res = Result(task=t)
        stage = "read"
        try:
            t0 = time.perf_counter()
            data = None
            size = os.path.getsize(t.abs_path)
            if size <= IN_MEMORY_READ_LIMIT:
                with open(t.abs_path, "rb") as f:
                    data = f.read()
            t0 = self._timed("read", t0)
            meta: dict = {}
            if t.need_meta:
                stage = "hash"
                meta["sha256"] = hashing.sha256_bytes(data) if data is not None else hashing.sha256_file(t.abs_path)
                t0 = self._timed("hash", t0)
            stage = "decode"
            try:
                dec = imaging.decode(t.abs_path, max_side=WORK_SIZE, data=data)
            except imaging.DecodeError as exc:
                if t.need_meta:
                    # Keep what we know (hash, file times) so the photo is still listed and deduplicated.
                    meta.update(metadata.extract(None, None, t.filename, t.folder, t.mtime, t.ctime, t.ext.upper().lstrip("."), 0, 0))
                    res.meta = meta
                res.error, res.error_stage = str(exc), "decode"
                return res
            data = None
            t0 = self._timed("decode", t0)
            img = dec.image
            res.orig_size = (dec.orig_width, dec.orig_height)
            if t.need_meta:
                stage = "metadata"
                meta.update(metadata.extract(dec.exif, dec.xmp, t.filename, t.folder, t.mtime, t.ctime,
                                             dec.format, dec.orig_width, dec.orig_height))
                meta.update(width=dec.orig_width, height=dec.orig_height, orientation=dec.orientation, format=dec.format)
                t0 = self._timed("metadata", t0)
                stage = "thumbnail"
                tp = imaging.thumb_path(self.ctx.paths.thumbs, meta["sha256"])
                if not tp.exists():
                    imaging.save_thumbnail(img, tp, self.ctx.settings.thumb_size)
                t0 = self._timed("thumbnail", t0)
                stage = "hash"
                meta["phash"], meta["dhash"] = hashing.perceptual_hashes(img)
                stage = "quality"
                meta.update(quality.image_quality(img))
                t0 = self._timed("phash+quality", t0)
                res.meta = meta
            if t.need_faces and self.face_engine is not None:
                stage = "face-preprocess"
                res.work = np.asarray(img, dtype=np.uint8)
                # Letterbox on the worker thread; the GPU thread stays free for inference.
                res.det_input = self.face_engine.prepare(res.work)
                t0 = self._timed("face-preprocess", t0)
            if t.need_semantic and self.sem_model is not None:
                stage = "semantic-preprocess"
                res.sem_input = self.sem_model.preprocess(img)
                self._timed("sem-preprocess", t0)
            return res
        except MemoryError:
            res.error, res.error_stage = "Out of memory while processing image", stage
            return res
        except Exception as exc:
            res.error, res.error_stage = f"{type(exc).__name__}: {exc}", stage
            log.debug("CPU stage failed for %s\n%s", t.abs_path, traceback.format_exc())
            return res

    # ------------------------------------------------------------------ GPU stage
    def _gpu_loop(self, gpu_q: queue.Queue, write_q: queue.Queue) -> None:
        """Batches GPU work across photos: one ArcFace call and one image-encoder call per batch."""
        from ..vision.device import gpu_batch_sizes, gpu_memory_gb

        batch: list[Result] = []
        min_size = self.ctx.settings.face_min_size_px
        min_score = self.ctx.settings.face_min_det_score
        sizes = gpu_batch_sizes(self.ctx.device)
        batch_size = sizes["pipeline"]
        self._face_batch = sizes["faces"]
        small_gpu = self.ctx.device == "cuda" and gpu_memory_gb() < 5
        flushes = 0

        def run_faces(items: list[Result]) -> None:
            face_items = [(r.work, r.det_input, r.orig_size[0], r.orig_size[1]) for r in items]
            t0 = time.perf_counter()
            try:
                results = self.face_engine.analyze_batch(face_items, min_size, min_score,
                                                         embed_batch=getattr(self, "_face_batch", 64))
                for r, faces in zip(items, results):
                    r.faces = faces
            except Exception as exc:
                if "out of memory" in str(exc).lower():
                    self._empty_cuda_cache()
                log.warning("Batched face analysis failed (%s); retrying per photo", exc)
                for r in items:  # isolate the failure to the offending photo
                    try:
                        r.faces = self.face_engine.analyze(r.work, r.orig_size[0], r.orig_size[1], min_size,
                                                           min_score, prepared=r.det_input)
                    except Exception as exc2:
                        r.error, r.error_stage = f"faces: {exc2}", "faces"
            finally:
                for r in items:
                    r.work = None
                    r.det_input = None
            self._timed("gpu-faces", t0)

        def run_semantic(items: list[Result]) -> None:
            t0 = time.perf_counter()
            try:
                embs = self.sem_model.encode_images(np.stack([r.sem_input for r in items]))
                for r, e in zip(items, embs):
                    r.embedding = e
            except Exception as exc:
                if "out of memory" in str(exc).lower():
                    self._empty_cuda_cache()
                log.warning("Batched embedding failed (%s); retrying per photo", exc)
                for r in items:
                    try:
                        r.embedding = self.sem_model.encode_images(r.sem_input[None])[0]
                    except Exception as exc2:
                        r.error, r.error_stage = f"semantic: {exc2}", "semantic"
            self._timed("gpu-semantic", t0)

        def flush():
            if not batch:
                return
            face_items = [r for r in batch if r.work is not None and self.face_engine is not None]
            if face_items:
                run_faces(face_items)
            sem_items = [r for r in batch if r.sem_input is not None and self.sem_model is not None]
            if sem_items:
                run_semantic(sem_items)
            for r in batch:
                r.sem_input = None
                r.work = None
                r.det_input = None
                self._put(write_q, r, force=True)
            batch.clear()

        while True:
            try:
                item = gpu_q.get(timeout=0.25)
            except queue.Empty:
                flush()
                continue
            if item is _SENTINEL:
                flush()
                break
            batch.append(item)
            if len(batch) >= batch_size:
                flush()
                flushes += 1
                # On a small card the allocator fragments badly over thousands of
                # batches; trimming occasionally is far cheaper than the thrashing.
                if small_gpu and flushes % 50 == 0:
                    self._empty_cuda_cache()

    @staticmethod
    def _empty_cuda_cache() -> None:
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:
            pass

    # ------------------------------------------------------------------ writer
    def _writer_loop(self, write_q: queue.Queue, total: int) -> None:
        conn = self.ctx.connect()
        done = 0
        pending = 0
        last_commit = time.time()
        last_cancel_check = 0.0
        t_begin = time.time()
        conn.execute("BEGIN")
        try:
            while True:
                try:
                    item = write_q.get(timeout=0.5)
                except queue.Empty:
                    item = None
                if item is _SENTINEL:
                    break
                if item is not None:
                    try:
                        self._write_result(conn, item)
                    except Exception as exc:
                        log.error("DB write failed for photo %s: %s", item.task.photo_id, exc, exc_info=True)
                        try:
                            conn.execute(
                                "INSERT INTO processing_errors(photo_id, path, stage, error, created_at) VALUES (?,?,?,?,?)",
                                (item.task.photo_id, item.task.abs_path, "write", str(exc)[:2000], time.time()),
                            )
                        except Exception:
                            pass
                    done += 1
                    pending += 1
                now = time.time()
                if pending and (pending >= 100 or now - last_commit > 2.0):
                    conn.commit()
                    conn.execute("BEGIN")
                    pending = 0
                    last_commit = now
                    rate = done / max(1e-6, now - t_begin)
                    eta = (total - done) / rate if rate > 0 else 0
                    self._report("analyze", done, total, f"{rate:.1f} photos/s · ETA {int(eta // 60)}m{int(eta % 60):02d}s")
                if now - last_cancel_check > 2.0:
                    last_cancel_check = now
                    if self._check_cancel(conn):
                        pass  # keep draining until the sentinel so in-flight work is saved
            conn.commit()
            self._report("analyze", done, total, "Analysis complete")
        finally:
            try:
                conn.commit()
            except Exception:
                pass
            conn.close()

    def _write_result(self, conn, r: Result) -> None:
        t = r.task
        now = time.time()
        pid = t.photo_id
        if r.meta is not None:
            m = r.meta
            # Move detection: a new path whose content matches a photo that went missing.
            if t.sha256 is None and m.get("sha256"):
                moved = conn.execute(
                    "SELECT id FROM photos WHERE sha256 = ? AND status = 'missing' AND id != ? LIMIT 1",
                    (m["sha256"], pid),
                ).fetchone()
                if moved:
                    old_id = int(moved[0])
                    row = conn.execute("SELECT root_id, rel_path, folder, filename, ext, size, mtime, ctime FROM photos WHERE id=?", (pid,)).fetchone()
                    conn.execute("DELETE FROM photos WHERE id=?", (pid,))
                    conn.execute(
                        "UPDATE photos SET root_id=?, rel_path=?, folder=?, filename=?, ext=?, size=?, mtime=?, ctime=?, "
                        "status = CASE WHEN error IS NULL THEN 'ok' ELSE 'error' END, last_seen_at=? WHERE id=?",
                        (*tuple(row), now, old_id),
                    )
                    db.audit(conn, "photo_moved", "photo", old_id, {"to": row["rel_path"]}, actor="indexer")
                    self.stats["moved"] += 1
                    return
            cols = [
                "sha256", "phash", "dhash", "width", "height", "orientation", "format", "taken_ts", "taken_local",
                "tz_offset_min", "date_source", "date_confidence", "camera_make", "camera_model", "lens",
                "focal_length", "aperture", "exposure_time", "iso", "software", "gps_lat", "gps_lon", "gps_alt",
                "source_kind", "blur", "brightness", "contrast", "clipped",
            ]
            vals = [m.get(c) for c in cols]
            location_cols = ""
            if m.get("gps_lat") is not None:
                location_cols = ", location_source='gps', location_confidence='high'"
            conn.execute(
                f"UPDATE photos SET {', '.join(c + '=?' for c in cols)}, meta_version=?, status=?, error=?{location_cols} WHERE id=?",
                (*vals, META_VERSION, "error" if r.error else "ok", r.error, pid),
            )
            self.stats["processed"] += 1
        if r.error:
            self.stats["errors"] += 1
            conn.execute(
                "INSERT INTO processing_errors(photo_id, path, stage, error, created_at) VALUES (?,?,?,?,?)",
                (pid, t.abs_path, r.error_stage or "unknown", r.error[:2000], now),
            )
            # Mark analysis stages as attempted so a permanently broken file is not retried on every run
            # (a changed file gets reset by the scanner; `--retry-errors` forces a retry).
            conn.execute(
                "UPDATE photos SET status='error', error=?, meta_version=COALESCE(meta_version, ?), "
                "faces_model=COALESCE(?, faces_model), semantic_model=COALESCE(?, semantic_model) WHERE id=?",
                (r.error[:500], META_VERSION, self.face_mid if t.need_faces else None,
                 self.sem_mid if t.need_semantic else None, pid),
            )
            return
        if r.faces is not None and self.face_mid is not None:
            self._write_faces(conn, pid, r.faces, now)
            self.stats["faces"] += len(r.faces)
        if r.embedding is not None and self.sem_mid is not None:
            conn.execute(
                "INSERT OR REPLACE INTO photo_embeddings(photo_id, model_id, vec) VALUES (?,?,?)",
                (pid, self.sem_mid, r.embedding.astype(np.float16).tobytes()),
            )
            conn.execute("UPDATE photos SET semantic_model=? WHERE id=?", (self.sem_mid, pid))

    def _write_faces(self, conn, pid: int, faces: list, now: float) -> None:
        old = conn.execute(
            "SELECT id, x1, y1, x2, y2, person_id, assign_source FROM faces WHERE photo_id=? AND model_id=?",
            (pid, self.face_mid),
        ).fetchall()
        # Preserve user-confirmed identities when a changed file is re-analysed (match by box IoU).
        carry: dict[int, tuple[int, str]] = {}
        if old:
            for i, f in enumerate(faces):
                best, best_iou = None, 0.5
                for o in old:
                    iou = _iou(f.box_norm, (o["x1"], o["y1"], o["x2"], o["y2"]))
                    if iou > best_iou and o["person_id"] is not None and o["assign_source"] == "user":
                        best, best_iou = o, iou
                if best is not None:
                    carry[i] = (best["person_id"], "user")
            conn.execute("DELETE FROM faces WHERE photo_id=? AND model_id=?", (pid, self.face_mid))
        for i, f in enumerate(faces):
            person, source = carry.get(i, (None, None))
            conn.execute(
                "INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, landmarks, det_score, size_px, sharpness, yaw, "
                "quality, embedding, person_id, assign_source, assign_confidence, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, self.face_mid, *f.box_norm, json.dumps(f.kps_norm), f.det_score, f.size_px, f.sharpness,
                 f.yaw, f.quality, f.embedding.astype(np.float16).tobytes(), person, source,
                 1.0 if person else None, now),
            )
        conn.execute("UPDATE photos SET faces_model=?, face_count=? WHERE id=?", (self.face_mid, len(faces), pid))


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0
