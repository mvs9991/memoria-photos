"""Calibrate zero-shot tag thresholds against COCO instance annotations.

SigLIP's absolute match probabilities are tiny for short prompts (p99 ~ 0.001),
so an absolute threshold is useless. This script measures precision/recall for
per-tag standardised scores (z-scores across the corpus), which is what the
tagger actually uses.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from photointel.context import AppContext  # noqa: E402
from photointel.engine.tags import tag_vectors  # noqa: E402
from photointel.vectors import normalize  # noqa: E402

DATASETS = Path(os.environ.get("PI_DATASETS", "D:/pi_cache/datasets"))
COCO_DIR = DATASETS / "val2017"
INSTANCES = DATASETS / "annotations/instances_val2017.json"
CACHE = DATASETS / "coco_sem_emb.npz"

MAPPING = {
    "dog": ["dog"], "cat": ["cat"], "cake": ["cake"], "car": ["car"], "boat": ["boat"],
    "train": ["train"], "laptop": ["laptop"], "horse": ["horse"], "elephant": ["elephant"],
    "cow": ["cow"], "bird": ["bird"], "motorcycle": ["motorcycle"], "bicycle": ["bicycle"],
    "book": ["book"], "flowers": ["potted plant"],
    "food": ["pizza", "sandwich", "hot dog", "donut", "broccoli", "carrot", "banana", "apple", "orange"],
}


def embed_images(model, images) -> np.ndarray:
    if CACHE.exists():
        d = np.load(CACHE, allow_pickle=True)
        if len(d["ids"]) == len(images):
            print(f"using cached embeddings {d['emb'].shape}")
            return d["emb"].astype(np.float32)
    embs, batch = [], []
    t0 = time.time()
    for n, (_, path) in enumerate(images):
        batch.append(model.preprocess(Image.open(path).convert("RGB")))
        if len(batch) >= 32:
            embs.append(model.encode_images(np.stack(batch)))
            batch = []
        if n and n % 500 == 0:
            print(f"  {n} ({n / (time.time() - t0):.1f} img/s)")
    if batch:
        embs.append(model.encode_images(np.stack(batch)))
    emb = normalize(np.concatenate(embs))
    np.savez(CACHE, emb=emb.astype(np.float16), ids=np.array([i for i, _ in images]))
    print(f"embedded {len(emb)} images in {time.time() - t0:.0f}s")
    return emb


def sweep(name: str, score: np.ndarray, thresholds, images, img_cats, tag_index) -> None:
    print(f"\n== {name} ==")
    print(f"{'tag':<12} {'pos':>5} " + " ".join(f"{('%.2f' % t):>13}" for t in thresholds))
    agg = {t: {"tp": 0, "fp": 0, "fn": 0} for t in thresholds}
    for tag, coco_cats in MAPPING.items():
        if tag not in tag_index:
            continue
        col = score[:, tag_index[tag]]
        truth = np.array([bool(img_cats[i] & set(coco_cats)) for i, _ in images])
        cells = []
        for t in thresholds:
            pred = col >= t
            tp = int((pred & truth).sum())
            fp = int((pred & ~truth).sum())
            fn = int((~pred & truth).sum())
            agg[t]["tp"] += tp
            agg[t]["fp"] += fp
            agg[t]["fn"] += fn
            cells.append(f"P{tp / max(tp + fp, 1):.2f}/R{tp / max(tp + fn, 1):.2f}")
        print(f"{tag:<12} {int(truth.sum()):5d} " + " ".join(f"{c:>13}" for c in cells))
    cells = []
    for t in thresholds:
        a = agg[t]
        p = a["tp"] / max(a["tp"] + a["fp"], 1)
        r = a["tp"] / max(a["tp"] + a["fn"], 1)
        f1 = 2 * p * r / (p + r) if p + r else 0
        cells.append(f"P{p:.2f}/R{r:.2f}")
    print(f"{'OVERALL':<12} {'':5} " + " ".join(f"{c:>13}" for c in cells))
    print("  F1: " + " ".join(
        f"{t}:{(2 * (agg[t]['tp'] / max(agg[t]['tp'] + agg[t]['fp'], 1)) * (agg[t]['tp'] / max(agg[t]['tp'] + agg[t]['fn'], 1)) / max(1e-9, (agg[t]['tp'] / max(agg[t]['tp'] + agg[t]['fp'], 1)) + (agg[t]['tp'] / max(agg[t]['tp'] + agg[t]['fn'], 1)))):.2f}"
        for t in thresholds))
    print("  avg tags/photo: " + " ".join(f"{t}:{float((score >= t).sum(axis=1).mean()):.1f}" for t in thresholds))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=int, default=1500)
    ap.add_argument("--data", default="data")
    args = ap.parse_args()

    inst = json.loads(INSTANCES.read_text(encoding="utf-8"))
    cat_name = {c["id"]: c["name"] for c in inst["categories"]}
    img_cats: dict[int, set[str]] = defaultdict(set)
    for a in inst["annotations"]:
        if a.get("area", 0) > 2500:
            img_cats[a["image_id"]].add(cat_name[a["category_id"]])
    images = [(im["id"], COCO_DIR / im["file_name"]) for im in inst["images"]][: args.images]
    images = [(i, p) for i, p in images if p.exists()]
    print(f"{len(images)} COCO images")

    ctx = AppContext(args.data)
    model = ctx.semantic_model()
    names, tvecs = tag_vectors(model)
    tag_index = {n: i for i, (n, _) in enumerate(names)}
    emb = normalize(embed_images(model, images).astype(np.float32))
    sims = emb @ tvecs.T
    probs = model.probability(sims)
    print(f"raw probability: max={probs.max():.4f} mean={probs.mean():.5f} p99={np.percentile(probs, 99):.4f}")
    print(f"raw similarity : max={sims.max():.3f} mean={sims.mean():.3f} p99={np.percentile(sims, 99):.3f}")

    mu, sd = sims.mean(axis=0, keepdims=True), sims.std(axis=0, keepdims=True) + 1e-6
    z = (sims - mu) / sd
    sweep("per-tag z-score", z, [1.5, 2.0, 2.5, 3.0, 4.0, 5.0], images, img_cats, tag_index)

    # Also try: z-score combined with a per-photo relative gate (tag must stand out for this photo).
    pmu, psd = sims.mean(axis=1, keepdims=True), sims.std(axis=1, keepdims=True) + 1e-6
    zp = (sims - pmu) / psd
    combo = np.minimum(z, zp)
    sweep("min(per-tag z, per-photo z)", combo, [1.5, 2.0, 2.5, 3.0, 4.0], images, img_cats, tag_index)

    print("\nz-score distribution by vocabulary category (max z per photo):")
    for cat in sorted({c for _, c in names}):
        cols = [i for i, (_, c) in enumerate(names) if c == cat]
        mx = z[:, cols].max(axis=1)
        print(f"  {cat:<11} median={np.median(mx):.2f} p75={np.percentile(mx, 75):.2f} "
              f"p95={np.percentile(mx, 95):.2f} max={mx.max():.2f}")


if __name__ == "__main__":
    main()
