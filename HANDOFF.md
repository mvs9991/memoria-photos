# Handoff notes

For whoever picks this up next — human or a fresh Claude session on another machine.

`README.md` documents the product. This file documents everything *around* it: why things
are the way they are, what was tried and rejected, what the measured numbers actually mean,
and what is still unfinished. Read it before changing anything in `photointel/engine/` or
the clustering thresholds.

---

## 1. What this is

**Memoria** — a local photo intelligence system. Point it at a folder of photos; it works out
who is in them, when and where they were taken, what they show, which are duplicates, and
groups them into events and trips. It then serves a photo web app for browsing and
natural-language search.

Hard constraints from the original brief, all still binding:

- **Originals are never modified, moved or deleted.** Everything written goes under one data
  directory. Photos are opened read-only. The Duplicates screen only ever *hides*.
- **No LLM in the recognition path.** Face identity is SCRFD + ArcFace + graph clustering. The
  optional Claude layer only parses queries it cannot parse with rules, and only ever returns
  the same structured filter the rule parser would.
- **Local first.** Reverse geocoding is offline; map tiles are off by default; the Claude layer
  is off by default. Nothing leaves the machine unless explicitly enabled.
- **Do not fabricate accuracy claims.** Every number in the README is measured, with the dataset
  named. Where something cannot be measured, it says so.

---

## 2. Architecture

```
photos ──► scan ──► decode / EXIF / hash / thumbnail ──► faces ──► embeddings
                                                            │          │
                                                            ▼          ▼
                                    SQLite  ◄──  clustering, events, places, duplicates
                                       │
                                       ▼
                             search  ──►  web app
```

```
photointel/
  config.py context.py db.py schema.sql        settings, app context, database
  imaging.py metadata.py hashing.py quality.py decode, EXIF, hashes, quality
  geo.py geodata.py                            offline reverse geocoding (GeoNames)
  vision/    faces.py semantic.py captioner.py vocab.py device.py
  pipeline/  scanner.py indexer.py post.py jobs.py
  engine/    clustering.py people.py events.py places.py duplicates.py tags.py
  search/    parser.py engine.py llm.py
  api/       app.py routes_*.py images.py
web/         React + TypeScript UI (Vite)
eval/        dataset builders, calibration, evaluation, library inspector
tests/       126 tests, no GPU required
```

**The indexing pipeline** is a feeder thread → N CPU worker threads (read, hash, decode, EXIF,
thumbnail, pHash, quality, preprocess) → one GPU stage (batched faces + embeddings) → one DB
writer committing in small transactions. One GPU stage and one writer are deliberate: they are
the serialisation points, and batching there is what makes the GPU the bottleneck rather than
Python overhead.

**Models.** SCRFD-10G detection and ArcFace R50 (buffalo_l) were converted from ONNX to PyTorch
with `onnx2torch`, verified numerically against onnxruntime (max abs diff ~1e-5), then
TorchScript-traced and frozen (~25% faster). This was necessary because onnxruntime-gpu ships
built for CUDA 13 and is unusable on this driver. Semantics are SigLIP2 ViT-B/16 via open_clip.
Captions are Florence-2-base, local, run only on selected photos.

---

## 3. Environment — **this part changes on a new machine**

Everything below is specific to the machine this was built on. Nothing under `photointel/` or
`web/src/` hardcodes any of it; the `eval/` scripts default to these dataset locations but take
`PI_DATASETS` as an override (see §8).

| What | Where it was here |
|---|---|
| Project | `D:\claude_photos_intelligence` |
| Python 3.11 venv | `.venv\Scripts\python.exe` (system Python was 3.9/EOL — do not use it) |
| Node 24 | `D:\pi_cache\tools\node-v24.21.0-win-x64` (not on PATH) |
| Playwright browsers | `D:\pi_cache\playwright` (set `PLAYWRIGHT_BROWSERS_PATH`) |
| Datasets | `D:\pi_cache\datasets` (COCO val2017 + annotations, LFW) |
| Demo/eval library | `D:\pi_cache\testlib`, indexed into `./data` |
| Scale library | `D:\pi_cache\scaledata` (COCO+LFW, 18,233 photos) |
| Real library | `D:\pi_cache\realdata` (23,488 personal photos) |

GPU here was a GTX 1050 Ti (4 GB, Pascal). Torch is `2.14.0+cu126` — **cu126 specifically**,
because cu128 builds dropped Pascal (sm_61) support. On a newer GPU you can install any current
torch build; nothing else depends on the CUDA version.

### Setting up on the new machine

Repository: **https://github.com/mvs9991/memoria-photos**

```bash
git clone https://github.com/mvs9991/memoria-photos.git
cd memoria-photos

python -m venv .venv                     # Python 3.11+
.venv/Scripts/activate
# PyTorch: pick the build for YOUR GPU — do not copy the cu126 pin below blindly.
#   https://pytorch.org/get-started/locally/  generates the right command.
#   cu126 was required *here* only because this machine had a Pascal card (GTX 1050 Ti);
#   newer CUDA builds dropped sm_61. On a modern GPU use the current default build.
#   CPU-only: just `pip install torch torchvision`.
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126

pip install -r requirements.txt

python -m photointel models download     # face models, ~280 MB
python -m photointel geo-setup           # offline place names, ~30 MB
python -m photointel add-root "D:/Photos"
python -m photointel index               # SigLIP2 (~800 MB) downloads here, on first use
python -m photointel serve               # http://127.0.0.1:8765
```

Verified: a fresh clone of the repository runs the full test suite (126 passed) and boots the CLI
against a new empty library using only committed files.

### What the repository does *not* contain

None of this belongs in git; all of it is regenerated by the commands above.

| Not committed | How to get it | Why |
|---|---|---|
| `.venv/` | `pip install -r requirements.txt` | platform-specific, gigabytes |
| Model weights (~1.1 GB) | `models download`, plus SigLIP2 on first index | binary blobs, licence-bound |
| GeoNames data (~30 MB) | `geo-setup` | regenerable download |
| `data/` — the index, thumbnails, database | re-run `index` | 2.9 GB here, and it is the user's photos |
| `web/node_modules/` | `npm install` in `web/` | **only needed to rebuild the UI** — `web/dist` *is* committed, so the app runs without Node |
| COCO / LFW eval datasets | download separately, then set `PI_DATASETS` | several GB of third-party data; only needed to re-run the accuracy benchmarks |
| `settings.json` | recreated on first run | may hold an Anthropic API key |

Re-indexing is the slow part, not the setup: 23,488 photos took ~65 minutes on a GTX 1050 Ti. It is
resumable, so interrupting it is safe.

### Environment variables

| Variable | Meaning |
|---|---|
| `PHOTOINTEL_DATA` | the library's data directory (default `./data`) |
| `PHOTOINTEL_MODELS` | model weights — **share this across libraries**, they are machine-level |
| `PHOTOINTEL_GEO` | offline place data — likewise shared |
| `PHOTOINTEL_DEVICE` | `cuda` / `cpu` / `auto` for one run, without editing settings |
| `PHOTOINTEL_DEV=1` | allow the Vite dev server cross-origin access (off by default) |

`PHOTOINTEL_MODELS`/`PHOTOINTEL_GEO` exist because a second library used to crash on a fresh data
dir demanding its own 280 MB copy of the weights.

### Moving the libraries

The data directory is self-contained and relocatable **except** that `roots.path` in `library.db`
stores absolute photo paths. If the photo folder moves, either re-run `add-root` with the new
path and re-index (analysis is keyed on content hash, so unchanged files are recognised as
*moves* and keep their faces and people — see `test_moved_file_keeps_its_analysis`), or update
`roots.path` directly. The thumbnail cache is keyed by sha256 and stays valid.

---

## 4. Decisions you should not casually reverse

Each of these was measured. The reasoning is not obvious from the code alone.

**Chinese Whispers, not connected components,** for face clustering. A single bad edge cannot
chain two identities: a face adopts only the label with the strongest total support.

**`merge_threshold = 0.68`** in `engine/clustering.py`. 0.55 sits on a percolation cliff. On the
real 56,617-face library it chained separate people into one 14,321-face "person" (mean
intra-similarity 0.26, 69% of pairs below cosine 0.3). Chosen by sweeping the *share of faces
landing in incoherent clusters*: 5.6% at 0.64, 0.6% at 0.68, no further gain above and steadily
more fragmentation. LFW BCubed F1 is unaffected (0.997 → 0.996). **Small libraries cannot show
this** — the failure needs enough faces for weak links to percolate.

**A person id may continue into only one cluster** (`engine/people.py::_map_clusters_to_persons`).
Otherwise a full recluster can never *repair* an over-merge: a bloated person claims a majority
in every cluster it contributed to and silently re-merges them. Symptom if this regresses:
`--full-recluster` reports a tiny `updated_faces` and the giant cluster survives.

**Low-quality faces can never create a person,** only attach to one afterwards at a higher
threshold. Keeps blurry/tiny/profile faces from seeding junk identities.

**Tag scores are the minimum of two z-scores** (per-tag across the library, and per-photo).
SigLIP's raw probabilities are useless as an absolute threshold — p99 ≈ 0.001 on COCO.

**Visual search cutoff is adaptive** (`SEM_HEAD_FRACTION = 0.60`). A fixed sigma keeps a fixed
*fraction* of the library, so "elephants" and "a bus" returned the same count regardless of how
many actually matched. Measured: precision 0.39 → 0.78 with p@20 unchanged at 0.97.

**A broad tag must not hard-filter a query.** "someone playing tennis" matched the tag *Playing*
and demoted "tennis" to a leftover; recall was 0.19. With no structured filter to anchor it the
tag is now advisory and the whole phrase drives the visual search — recall 0.95, p@20 1.00.

**Events compare cities, not place ids,** when merging same-day segments — a day out resolves to
many different neighbourhood records.

**Event titles prefer a plain name to a wrong one.** A category is used only when it clearly
beats the runner-up. Folder hints reject connectives: Google Takeout names folders
`Photos from 2011`, and stripping the generic word and the year left the title **"From"**, which
became the most common event name in a real library (148 events).

**Google Takeout `.json` sidecars are deliberately ignored.** Measured across 6,415 sidecars: the
date already derived from EXIF or filename agreed with `photoTakenTime` 98.6% of the time, and in
the disagreements the sidecar was *worse* — WhatsApp images dated 2018-09-14 by filename carried a
Google timestamp of 2020-01-08 15:46, seconds apart across many files, i.e. a bulk upload, not a
capture. Do not "fix" this without re-measuring.

**`.nomedia` is deliberately not honoured.** WhatsApp puts it in `Sent`, which held 1,339 of the
owner's own photos. Dot-directories are already excluded, which handles real app-cache junk.

**Reclaimable bytes exclude `similar` groups.** Those are different photographs that merely look
alike; deleting them reclaims nothing you had twice.

---

## 5. Measured numbers, and what they are worth

All reproducible via `eval/`. The datasets are named because the numbers only describe them.

| Dataset | Result |
|---|---|
| LFW (13,185 faces) | TAR@FAR 1e-4 **99.6%**; clustering 60 recurring among 800 strangers, BCubed F1 **0.997** |
| Synthetic library (1,490 photos, ground truth) | people P/R/F1 **0.987 / 0.966 / 0.976**; events trip-level P/R **1.00 / 1.00**; duplicates P/R **0.89 / 0.88**; city accuracy 92.7%; structured search P/R **0.94 / 0.99** |
| COCO val2017, object queries (20) | **p@20 0.97**, macro P/R 0.78 / 0.77 |
| COCO val2017, scene/occasion queries (12) | **p@20 0.72**, recall 0.87 — a *lower bound*, see below |
| Real library (23,488 photos, 56,617 faces) | 100% indexed, zero failures; ~6 photos/s; clustering 33.6 s; 2.2% of faces in incoherent clusters |
| Synthetic 100k photos / 150k faces | clustering 103 s, duplicates 99 s, vector search 0.01 s, DB 740 MB |

Caveats that matter:

- **Scene-query scores are a lower bound.** Caption-derived ground truth only contains photos whose
  describers used the word. The top hits for "animals at the zoo" are zebras and giraffes behind
  enclosure fencing, and for "birthday party" cakes with lit candles — all correct, nearly all
  outside the ground truth. The keyword lists were deliberately *not* widened to match what the
  engine returned, which would only measure how well the ground truth had been fitted to the answer.
- **Cluster coherence is a proxy, not truth.** Nobody has labelled who is actually who in the real
  library. "Mean intra-similarity 0.72" says a cluster is internally consistent, not that it is the
  right person.
- **The synthetic library composites LFW faces onto unrelated COCO scenes.** Its "wedding" photos
  show coffee cups. `eval/evaluate.py` marks those rows ungradable and excludes them from averages
  rather than reporting a meaningless number. Real visual search is measured on COCO instead.

---

## 6. The recurring trap on this project

**Four separate times, a "failing" metric turned out to be a defect in the evaluation, not the
system.** Before changing engine code because a number looks bad, open an actual failing example —
view the image, print the ground-truth record — and confirm the expected answer is really correct.

1. Windows path separators in the synthetic ground truth made correct matches read as false positives.
2. The synthetic library's event labels say nothing about its pixels (above).
3. Requiring a COCO object to cover ≥2% of the frame cut tennis-racket ground truth from 167 images
   to 43 and skis from 120 to 17, marking correct results as errors. Ground truth is presence-based now.
4. The library inspector counted duplicate *membership rows* rather than distinct photos and reported
   "152% of photos are duplicates".

Equally: **a regression test that passes before and after the fix is worthless.** The first test
written for the clustering cliff passed at both 0.55 and 0.68. Always verify a new regression test
actually fails against the old behaviour.

---

## 7. Bugs found only by running on a real library

The synthetic and LFW benchmarks reported everything as near-perfect. A real 23,488-photo library
found seven defects. This is the argument for repeating the exercise after significant changes.

1. **Grid virtualisation was entirely broken** — 12,303 tiles mounted at once (42,262 DOM nodes).
   The cause was three layers up in CSS: `.shell` is a grid whose auto-sized row grows to fit
   content, so `height: 100%` acted as a minimum, `.content` never became a scroll container, and
   the grid measured its viewport as 634,813px — so its overscan band covered the whole library.
   Fixed with `overflow: hidden` on the shell and `min-height: 0` down the flex chain.
2. **Face clustering chained identities** (§4).
3. **A full recluster could not repair an over-merge** (§4). Only caught because fixing the
   threshold changed nothing.
4. **"From" was the most common event title** (§4).
5. **Model weights were per-library**, so a second library crashed instead of sharing them.
6. **CORS permanently trusted the Vite dev origin** even in normal runs.
7. **Duplicates reported reclaimable bytes for the current page only** — 1.8 GB shown next to
   "8,732 groups" when the real figure was 33.9 GB.

Plus the People page rendering all 1,790 discovered people at once (9,229 DOM nodes, ~7 s load);
it now shows 300 with a reveal control.

---

## 8. Evaluation tooling

```bash
# ground-truth library + end-to-end evaluation
python eval/make_test_library.py --out <dir>/testlib --clean
python -m photointel --data <dir>/testdata add-root <dir>/testlib
python -m photointel --data <dir>/testdata index
python eval/evaluate.py --data <dir>/testdata --library <dir>/testlib

# visual + scene search against COCO's own annotations
python eval/eval_visual_search.py --data <dir>/scaledata

# post-analysis scale timings (synthetic DB, no images or models needed)
python eval/scale_benchmark.py --photos 100000 --faces 150000

# sanity report for a REAL library, where there is no answer key
python eval/inspect_library.py --data ./data
```

`inspect_library.py` is the one to run on the new machine after re-indexing. It prints
distributions and flags the failure modes that matter — a cluster swallowing the library, dates in
the future, everything collapsing into one event, fuzzy duplicate thresholds too loose.

> **Dataset locations** default to `D:/pi_cache/datasets` and are overridden with one variable:
> `PI_DATASETS=/path/to/datasets`. (`COCO_DIR` and `LFW_DIR` still override individually.) Nothing
> under `photointel/` or `web/src/` contains an absolute path — only `eval/`, and only through
> that default.

---

## 9. Still open

- **RAW decoding is unverified.** The code path exists (embedded preview, else demosaic) and its
  failure handling is tested, but no camera RAW file existed on the build machine, so no
  CR2/NEF/ARW/DNG has ever actually been decoded. **If the new machine has RAW files, this is the
  first thing worth testing.**
- **Scene/occasion search accuracy** is known only as a lower bound (§5).
- **Face thresholds** were calibrated on LFW (adult celebrity portraits) then corrected against one
  real library. A library of young children changing year to year may need re-tuning; sweep with
  the method in §4 rather than guessing.
- 2.2% of faces still sit in clusters that mix identities. The merge/split tools exist because no
  threshold gets this perfect.
- Cosmetic: folder tokens of ≤3 letters are uppercased as probable airport/city codes (BLR, HYD),
  so `Goa Trip 2019` titles as `GOA Trip`. Changing it risks breaking real codes.

---

## 10. Working notes

- Run the suite with `.venv/Scripts/python.exe -m pytest -q`. 126 tests, ~95 s, no GPU needed —
  the neural nets are replaced by deterministic fakes.
- Test fixtures seed randomness from `zlib.crc32` of the **file name**, not `hash()` (salted per
  process) and not the full path (contains pytest's per-run tmp counter). Both made failures
  irreproducible.
- The UI review tool is `web/shots.mjs` — it screenshots every screen, fails on console errors, any
  failed API request, and now asserts the grid is virtualising. Run it after UI changes:
  `node shots.mjs <outdir> http://127.0.0.1:8765`. It waits for images to decode and for animations
  to settle, because an early capture once produced blank tiles and a half-counted "279 photos" on a
  1,489-photo library that looked exactly like a rendering bug.
- `npm run build` in `web/` rebuilds the SPA into `web/dist`, which the server serves.
- Long index runs should be started as a background task directly, not as `cmd & sleep; tail` — the
  child dies with the shell.
- Indexing is resumable and safe to interrupt; `index` re-processes only what changed. An
  `index.lock` in the data directory prevents two concurrent runs (it takes over a lock whose pid is
  gone).
