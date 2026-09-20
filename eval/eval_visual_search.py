"""Measure *visual* search quality against real pixel-level ground truth.

The synthetic test library cannot measure this: its "wedding" and "beach" photos
are arbitrary COCO scenes with faces composited on top, so the event label says
nothing about what the pixels show. Anything measured there would grade the
generator, not the search engine.

COCO val2017 has human-annotated instance labels for the actual image content,
so indexing it and querying it through the real search engine gives an honest
number for "does a natural-language visual query return the right photos?".

    python eval/eval_visual_search.py --data D:/pi_cache/scaledata

Requires a library that has val2017 indexed (see README > Evaluation).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from photointel.context import AppContext  # noqa: E402
from photointel.search.engine import SearchEngine  # noqa: E402

DATASETS = Path(os.environ.get("PI_DATASETS", "D:/pi_cache/datasets"))
INSTANCES = DATASETS / "annotations/instances_val2017.json"

# Natural-language queries a person would actually type, paired with the COCO
# categories that make a photo a correct answer. Kept deliberately varied:
# single objects, animals, food, vehicles, scenes and an activity.
#
# Caveat, because it bounds what these numbers mean: COCO labels *objects*, not
# scenes. "beach photos" has no COCO category, so it is scored against a
# surfboard/umbrella proxy that both misses sand-and-sea photos and counts
# rainy-street umbrellas as beaches. That row understates real quality and is
# kept visible rather than dropped; the object-grounded rows are the sound ones.
OBJECT_QUERIES: list[tuple[str, tuple[str, ...]]] = [
    ("photos of a dog", ("dog",)),
    ("cat pictures", ("cat",)),
    ("pizza", ("pizza",)),
    ("photos with a train", ("train",)),
    ("people riding horses", ("horse",)),
    ("surfing photos", ("surfboard",)),
    ("beach photos", ("surfboard", "umbrella")),
    ("skiing and snowboarding", ("skis", "snowboard")),
    ("bicycles", ("bicycle",)),
    ("boats on the water", ("boat",)),
    ("airplane photos", ("airplane",)),
    ("birthday cake", ("cake",)),
    ("elephants", ("elephant",)),
    ("a laptop on a desk", ("laptop",)),
    ("traffic lights in a city street", ("traffic light",)),
    ("someone playing tennis", ("tennis racket",)),
    ("flowers in a vase", ("vase",)),
    ("a bus", ("bus",)),
    ("pizza and wine", ("pizza", "wine glass")),
    ("teddy bear", ("teddy bear",)),
]

# Ground truth is presence-based: an image counts if COCO annotated the category
# in it at all. An earlier version required the object to cover >=2% of the frame,
# on the theory that a tiny background object is not what the photo is "about".
# That was wrong for small objects — it cut tennis-racket ground truth from 167
# images to 43 and skis from 120 to 17, marking correct results as errors. Raise
# --min-area to restore that behaviour; 0 is the honest default.
MIN_AREA_FRACTION = 0.0

CAPTIONS = DATASETS / "annotations/captions_val2017.json"

# Scene and occasion queries — "is this a wedding?", not "is there a racket?".
# COCO's *instance* labels cannot grade these (there is no wedding category), but
# its five human captions per image describe the scene, so a word appearing in at
# least two independent captions is solid evidence the scene really is that.
# Requiring two captions rather than one drops single-annotator noise.
#
# These scores are a LOWER BOUND, and materially so for the rarer scenes. Caption
# ground truth only catches photos whose describers happened to use the word: the
# top hits for "animals at the zoo" are zebras and giraffes in fenced enclosures
# and for "birthday party" are cakes with lit candles and children in party hats —
# all correct, nearly all outside the ground truth because no caption says "zoo"
# or "birthday". The keyword lists are deliberately left narrow rather than being
# widened to whatever the engine happened to return, which would only measure how
# well the ground truth was fitted to the answer.
SCENE_QUERIES: list[tuple[str, tuple[str, ...]]] = [
    ("beach photos", ("beach",)),
    ("a wedding", ("wedding", "bride", "groom")),
    ("birthday party", ("birthday",)),
    ("playing in the snow", ("snow",)),
    ("a kitchen", ("kitchen",)),
    ("a bathroom", ("bathroom",)),
    ("eating at a restaurant", ("restaurant", "diner")),
    ("a baseball game", ("baseball",)),
    ("a living room", ("living room",)),
    ("city at night", ("night",)),
    ("at the airport", ("airport",)),
    ("animals at the zoo", ("zoo",)),
]
MIN_CAPTIONS = 2


def load_scene_ground_truth(min_captions: int) -> dict[str, set[str]]:
    """Scene ground truth from COCO captions: {keyword: {file_name, ...}}."""
    import re
    from collections import defaultdict as _dd

    data = json.loads(CAPTIONS.read_text(encoding="utf-8"))
    names = {im["id"]: im["file_name"] for im in data["images"]}
    caps: dict[int, list[str]] = _dd(list)
    for ann in data["annotations"]:
        caps[ann["image_id"]].append(ann["caption"].lower())
    out: dict[str, set[str]] = _dd(set)
    wanted = {kw for _, kws in SCENE_QUERIES for kw in kws}
    patterns = {kw: re.compile(r"\b" + re.escape(kw) + r"\b") for kw in wanted}
    for image_id, texts in caps.items():
        for kw, rx in patterns.items():
            if sum(1 for t in texts if rx.search(t)) >= min_captions:
                out[kw].add(names[image_id])
    return out


def load_ground_truth(min_area: float) -> tuple[dict[str, set[str]], set[str]]:
    data = json.loads(INSTANCES.read_text(encoding="utf-8"))
    cats = {c["id"]: c["name"] for c in data["categories"]}
    sizes = {im["id"]: (im["width"] * im["height"], im["file_name"]) for im in data["images"]}
    area_by_image_cat: dict[tuple[int, str], float] = defaultdict(float)
    for ann in data["annotations"]:
        if ann.get("iscrowd"):
            continue
        area_by_image_cat[(ann["image_id"], cats[ann["category_id"]])] += ann.get("area", 0.0)
    by_cat: dict[str, set[str]] = defaultdict(set)
    for (image_id, cat), area in area_by_image_cat.items():
        total, fname = sizes[image_id]
        if not min_area or (total and area / total >= min_area):
            by_cat[cat].add(fname)
    all_files = {f for _, f in sizes.values()}
    return by_cat, all_files


def run_suite(engine, conn, labelled, suite, gt_by_key, k, title) -> dict:
    """Score one query suite. Unlabelled photos are neither right nor wrong."""
    macro_p = macro_r = macro_pk = 0.0
    took: list[float] = []
    rows = []
    for query, keys in suite:
        gt_files: set[str] = set()
        for key in keys:
            gt_files |= gt_by_key.get(key, set())
        gt_ids = {pid for pid, fn in labelled.items() if fn in gt_files}
        if not gt_ids:
            print(f"  {query:<34} (no ground truth — skipped)")
            continue
        t0 = time.time()
        res = engine.search(conn, query, limit=500, use_llm=False)
        took.append((time.time() - t0) * 1000)
        ranked = [pid for pid in res.photo_ids if pid in labelled]
        hits = [pid for pid in ranked if pid in gt_ids]
        kk = min(k, len(ranked)) or 1
        p_at_k = sum(1 for pid in ranked[:k] if pid in gt_ids) / kk
        precision = len(hits) / len(ranked) if ranked else 0.0
        recall = len(hits) / len(gt_ids)
        macro_p += precision
        macro_r += recall
        macro_pk += p_at_k
        rows.append({"query": query, "gt": len(gt_ids), "returned": len(ranked),
                     "precision": round(precision, 3), "recall": round(recall, 3),
                     f"p@{k}": round(p_at_k, 3),
                     "interpretation": [i.get("label", i.get("kind")) for i in res.interpretation]})
        print(f"  {query:<34} P={precision:.2f} R={recall:.2f} p@{k}={p_at_k:.2f} "
              f"({len(ranked)}/{len(gt_ids)})")
    n = max(len(rows), 1)
    summary = {"queries": len(rows),
               "macro_precision": round(macro_p / n, 3),
               "macro_recall": round(macro_r / n, 3),
               f"macro_p@{k}": round(macro_pk / n, 3),
               "median_latency_ms": round(sorted(took)[len(took) // 2], 1) if took else None}
    print(f"\n--- {title} ---")
    for key, v in summary.items():
        print(f"  {key:<24} {v}")
    return {"summary": summary, "queries": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="D:/pi_cache/scaledata")
    ap.add_argument("--k", type=int, default=20, help="cutoff for precision@k")
    ap.add_argument("--min-area", type=float, default=MIN_AREA_FRACTION)
    ap.add_argument("--min-captions", type=int, default=MIN_CAPTIONS,
                    help="captions that must mention a scene word for it to count as ground truth")
    ap.add_argument("--suite", choices=["objects", "scenes", "both"], default="both")
    ap.add_argument("--json", help="write metrics to this file")
    args = ap.parse_args()

    if not INSTANCES.exists():
        print(f"COCO annotations not found at {INSTANCES}")
        return 1

    ctx = AppContext(args.data)
    conn = ctx.connect()

    # Only val2017 photos are annotated; everything else in the library is an
    # unlabelled distractor that must not be counted as a false positive.
    rows = conn.execute(
        "SELECT p.id, p.filename FROM photos p JOIN roots r ON r.id = p.root_id "
        "WHERE r.path LIKE '%val2017%' AND p.status = 'ok'").fetchall()
    labelled = {int(r[0]): r[1] for r in rows}
    if not labelled:
        print("No val2017 photos in this library; index it first.")
        return 1
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    print(f"Ground truth: {len(labelled)} annotated photos "
          f"(+{total - len(labelled)} unlabelled distractors)")

    engine = SearchEngine(ctx)
    out: dict = {}
    if args.suite in ("objects", "both"):
        by_cat, _ = load_ground_truth(args.min_area)
        print(f"\nOBJECT QUERIES — ground truth from COCO instance labels "
              f"(min area {args.min_area:.0%})")
        out["objects"] = run_suite(engine, conn, labelled, OBJECT_QUERIES, by_cat, args.k,
                                   "object queries")
    if args.suite in ("scenes", "both"):
        if not CAPTIONS.exists():
            print(f"\nCOCO captions not found at {CAPTIONS}; skipping scene queries")
        else:
            by_kw = load_scene_ground_truth(args.min_captions)
            print(f"\nSCENE / OCCASION QUERIES — ground truth from COCO captions "
                  f"(word in >= {args.min_captions} of 5 captions)")
            out["scenes"] = run_suite(engine, conn, labelled, SCENE_QUERIES, by_kw, args.k,
                                      "scene / occasion queries")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
