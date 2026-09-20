"""Embed the LFW dataset with the production face engine (cached to an .npz).

LFW is used only as an *evaluation* set for threshold calibration and regression
testing of the face pipeline. Download: https://vis-www.cs.umass.edu/lfw/
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photointel.vision.faces import FaceEngine  # noqa: E402

DATASETS = Path(os.environ.get("PI_DATASETS", "D:/pi_cache/datasets"))
LFW_DIR = Path(os.environ.get("LFW_DIR", DATASETS / "lfw_funneled"))
OUT = Path(os.environ.get("LFW_OUT", "D:/pi_cache/datasets/lfw_embeddings.npz"))
MODEL_DIR = Path("D:/claude_photos_intelligence/data/models/insightface/buffalo_l")


def main(limit_identities: int | None = None) -> None:
    people = sorted(p for p in LFW_DIR.iterdir() if p.is_dir())
    if limit_identities:
        people = people[:limit_identities]
    files, labels = [], []
    for i, person in enumerate(people):
        for f in sorted(person.glob("*.jpg")):
            files.append(f)
            labels.append(i)
    print(f"{len(files)} images, {len(people)} identities")

    fe = FaceEngine(MODEL_DIR, device="cuda")
    embs, keep_labels, quals, sizes, dets = [], [], [], [], []
    batch, batch_labels = [], []
    t0 = time.time()
    misses = 0

    def flush():
        nonlocal misses
        if not batch:
            return
        results = fe.analyze_batch(batch, min_size_px=20, min_score=0.5)
        for recs, lab in zip(results, batch_labels):
            if not recs:
                misses += 1
                continue
            # LFW is funneled: the subject is the most central face.
            recs.sort(key=lambda r: abs((r.box_norm[0] + r.box_norm[2]) / 2 - 0.5)
                      + abs((r.box_norm[1] + r.box_norm[3]) / 2 - 0.5))
            r = recs[0]
            embs.append(r.embedding)
            keep_labels.append(lab)
            quals.append(r.quality)
            sizes.append(r.size_px)
            dets.append(r.det_score)
        batch.clear()
        batch_labels.clear()

    for n, (f, lab) in enumerate(zip(files, labels)):
        img = np.asarray(Image.open(f).convert("RGB"))
        batch.append((img, None, img.shape[1], img.shape[0]))
        batch_labels.append(lab)
        if len(batch) >= 24:
            flush()
        if n % 2000 == 0 and n:
            el = time.time() - t0
            print(f"  {n}/{len(files)} {n/el:.1f} img/s")
    flush()

    np.savez(OUT, emb=np.stack(embs).astype(np.float16), label=np.array(keep_labels),
             quality=np.array(quals, dtype=np.float32), size_px=np.array(sizes, dtype=np.float32),
             det_score=np.array(dets, dtype=np.float32),
             names=np.array([p.name for p in people]))
    print(f"saved {OUT}: {len(embs)} faces, {misses} images with no detection, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
