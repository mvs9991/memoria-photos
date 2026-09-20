"""Scale benchmark for everything that runs *after* photo analysis.

Analysis speed is bounded by the GPU and measured elsewhere; what matters for a
large library is whether clustering, duplicate detection, event grouping and
search stay usable at 100k+ photos. This builds a synthetic database directly
(no image files, no models) and times each stage.

    python eval/scale_benchmark.py --photos 100000 --faces 150000
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from photointel import db  # noqa: E402
from photointel.context import AppContext  # noqa: E402
from photointel.metadata import naive_to_ts  # noqa: E402


class Timer:
    def __init__(self):
        self.rows: list[tuple[str, float]] = []

    def time(self, label: str, fn):
        t0 = time.time()
        out = fn()
        dt = time.time() - t0
        self.rows.append((label, dt))
        print(f"  {label:<38} {dt:8.2f}s   {out if isinstance(out, str) else ''}")
        return out

    def report(self):
        print("\n--- summary ---")
        for label, dt in self.rows:
            print(f"  {label:<38} {dt:8.2f}s")


def build(conn, n_photos: int, n_faces: int, n_people: int, dim_face: int, dim_sem: int, seed: int = 0):
    rng = np.random.default_rng(seed)
    now = datetime(2025, 1, 1)
    face_model = db.register_model(conn, "face", "bench-face", "1", dim_face)
    sem_model = db.register_model(conn, "semantic", "bench-sem", "1", dim_sem)
    db.set_active_model(conn, "face", face_model)
    db.set_active_model(conn, "semantic", sem_model)
    conn.execute("INSERT INTO roots(path, added_at) VALUES ('bench', 0)")

    # photos spread over 6 years in bursts of a few per day
    print(f"building {n_photos:,} photos / {n_faces:,} faces / {n_people:,} people ...")
    t0 = time.time()
    rows = []
    ts = naive_to_ts(now - timedelta(days=6 * 365))
    dup_every = 20            # 5% exact copies
    near_every = 25           # 4% near copies (same scene, a few bits apart)
    last_sha, last_phash, last_dhash, last_ts = None, None, None, None
    for i in range(n_photos):
        ts += float(rng.exponential(700))          # seconds between photos
        if i % 200 == 0:
            ts += float(rng.exponential(40000))    # occasional long gap -> event boundary
        w, h = (4032, 3024) if i % 3 else (3024, 4032)
        sha = f"{i:064x}"[:64]
        phash = int(rng.integers(-2**62, 2**62))
        dhash = int(rng.integers(-2**62, 2**62))
        taken = ts
        if last_sha and i % dup_every == 0:                      # exact copy
            sha, phash, dhash, taken = last_sha, last_phash, last_dhash, last_ts
        elif last_phash is not None and i % near_every == 0:     # near copy: flip 3 bits
            phash = last_phash ^ 0b111
            dhash = last_dhash ^ 0b11
            taken = last_ts
        else:
            last_sha, last_phash, last_dhash, last_ts = sha, phash, dhash, ts
        rows.append((1, f"folder{i % 400}/IMG_{i:07d}.jpg", f"folder{i % 400}", f"IMG_{i:07d}.jpg", ".jpg",
                     3_000_000, ts, ts, "ok", ts, ts, sha, phash,
                     dhash, w, h, 1, "JPEG", taken,
                     datetime.utcfromtimestamp(taken).strftime("%Y-%m-%d %H:%M:%S"), "exif", "high",
                     "phone" if i % 5 else "camera", 250.0, 0.5, 0.2, 0.01, 60.0, 1, face_model, sem_model))
    conn.executemany(
        """INSERT INTO photos(root_id, rel_path, folder, filename, ext, size, mtime, ctime, status,
           first_seen_at, last_seen_at, sha256, phash, dhash, width, height, orientation, format,
           taken_ts, taken_local, date_source, date_confidence, source_kind, blur, brightness, contrast,
           clipped, quality_score, meta_version, faces_model, semantic_model)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    photo_ids = [int(r[0]) for r in conn.execute("SELECT id FROM photos ORDER BY id")]

    # semantic embeddings: a few hundred "scene themes" so neighbours exist
    themes = rng.normal(size=(300, dim_sem)).astype(np.float32)
    themes /= np.linalg.norm(themes, axis=1, keepdims=True)
    batch = []
    for i, pid in enumerate(photo_ids):
        v = themes[i % 300] + rng.normal(scale=0.5, size=dim_sem).astype(np.float32)
        v /= np.linalg.norm(v)
        batch.append((pid, sem_model, v.astype(np.float16).tobytes()))
        if len(batch) >= 5000:
            conn.executemany("INSERT INTO photo_embeddings(photo_id, model_id, vec) VALUES (?,?,?)", batch)
            batch = []
    if batch:
        conn.executemany("INSERT INTO photo_embeddings(photo_id, model_id, vec) VALUES (?,?,?)", batch)
    conn.commit()

    # faces: n_people recurring identities plus one-off strangers
    centres = rng.normal(size=(n_people, dim_face)).astype(np.float32)
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    spread = float(np.sqrt((1.0 / 0.62 - 1.0) / dim_face))
    batch = []
    for i in range(n_faces):
        pid = photo_ids[int(rng.integers(0, len(photo_ids)))]
        if i % 5 == 0:                               # 20% strangers
            v = rng.normal(size=dim_face).astype(np.float32)
        else:
            v = centres[i % n_people] + rng.normal(scale=spread, size=dim_face).astype(np.float32)
        v /= np.linalg.norm(v)
        batch.append((pid, face_model, 0.2, 0.2, 0.4, 0.5, 0.9, 140.0, 0.8, time.time(),
                      v.astype(np.float16).tobytes()))
        if len(batch) >= 5000:
            conn.executemany(
                """INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality,
                   created_at, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", batch)
            batch = []
    if batch:
        conn.executemany(
            """INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality,
               created_at, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", batch)
    conn.commit()
    print(f"  built in {time.time() - t0:.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", type=int, default=100_000)
    ap.add_argument("--faces", type=int, default=150_000)
    ap.add_argument("--people", type=int, default=400)
    ap.add_argument("--data", default="D:/pi_cache/benchdata")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    data = Path(args.data)
    if data.exists() and not args.keep:
        shutil.rmtree(data)
    ctx = AppContext(data)
    conn = ctx.connect()
    if conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0:
        build(conn, args.photos, args.faces, args.people, dim_face=512, dim_sem=768)

    from photointel.engine import duplicates as dup_mod
    from photointel.engine import events as events_mod
    from photointel.engine import people as people_mod
    from photointel.pipeline.post import rebuild_fts
    from photointel.vectors import load_photo_embeddings, normalize

    print(f"\nbenchmark on {args.photos:,} photos / {args.faces:,} faces (device={ctx.device})")
    t = Timer()
    t.time("face clustering + assignment", lambda: str(people_mod.recluster(ctx, conn, full=True)))
    t.time("event detection", lambda: str(events_mod.detect_events(ctx, conn)))
    t.time("duplicate detection", lambda: str(dup_mod.find_duplicates(ctx, conn)))
    t.time("search index rebuild (FTS)", lambda: str(rebuild_fts(conn)))

    model_id = db.active_model_id(conn, "semantic")
    ids, mat = t.time("load embeddings into memory", lambda: load_photo_embeddings(conn, model_id))
    mat = normalize(mat)
    print(f"  embedding matrix: {mat.shape} = {mat.nbytes / 1e6:.0f} MB")

    q = mat[0]
    t.time("vector search (1 query, all photos)", lambda: str(np.argsort(-(mat @ q))[:200].shape))

    # a person-filtered query, the most common shape in the UI
    pid = conn.execute("SELECT person_id FROM faces WHERE person_id IS NOT NULL LIMIT 1").fetchone()[0]
    t.time("SQL: photos of one person", lambda: str(len(conn.execute(
        "SELECT DISTINCT photo_id FROM faces WHERE person_id = ?", (pid,)).fetchall())))
    t.time("SQL: photo index query (grid)", lambda: str(len(conn.execute(
        "SELECT id, width, height, taken_ts FROM photos WHERE status='ok' ORDER BY taken_ts DESC").fetchall())))
    t.time("SQL: timeline aggregation", lambda: str(len(conn.execute(
        """SELECT strftime('%Y', taken_ts, 'unixepoch') y, strftime('%m', taken_ts, 'unixepoch') m, COUNT(*)
           FROM photos WHERE status='ok' GROUP BY y, m""").fetchall())))

    t.report()
    size = (data / "library.db").stat().st_size
    print(f"\ndatabase: {size / 1e6:.0f} MB for {args.photos:,} photos "
          f"({size / max(args.photos, 1):.0f} bytes/photo)")
    print(f"people found: {conn.execute('SELECT COUNT(*) FROM persons WHERE face_count > 0').fetchone()[0]}")
    print(f"events: {conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()
