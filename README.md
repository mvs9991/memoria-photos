# Memoria — local photo intelligence

Point it at a folder of photos. It reads every image once, works out **who** is in them,
**when** and **where** they were taken, **what** they show, which ones are **duplicates**,
and groups them into **events and trips** — then gives you a photo app to browse and search
it all in plain language.

Everything runs on your machine. Your photos are never uploaded, modified, moved or deleted.

![Home](docs/screenshots/home.webp)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/people.webp" alt="People"></td>
<td width="50%"><img src="docs/screenshots/event.webp" alt="Event detail"></td>
</tr>
<tr>
<td><img src="docs/screenshots/duplicates.webp" alt="Duplicates"></td>
<td><img src="docs/screenshots/photos.webp" alt="Photo grid"></td>
</tr>
</table>

<sup>Screens shown on a synthetic test library (COCO scenes with LFW faces composited on), not
anyone's real photos.</sup>

## Why this exists

Photo libraries become unusable at scale. You know a photo exists — a particular person, a
particular trip — but finding it means scrolling through years. Cloud services solve this by
uploading everything. This solves it locally: the recognition, the clustering, the geocoding and
the search all run on your own machine, and the originals are only ever opened read-only.

```
photos ──► scan ──► decode / EXIF / hash / thumbnail ──► faces ──► embeddings
                                                            │          │
                                                            ▼          ▼
                                    SQLite  ◄──  clustering, events, places, duplicates
                                       │
                                       ▼
                             search  ──►  web app
```

---

## What it does

| | |
|---|---|
| **People** | Finds every face, groups them into people, and lets you name, merge, split and correct them. Your corrections are permanent constraints — re-clustering never undoes them. |
| **Events & trips** | Groups photos into events from time gaps, GPS jumps, folders, people and scenery; multi-day journeys away from home become trips. |
| **Places** | Offline reverse geocoding (GeoNames) — country → region → city → neighbourhood, plus landmarks. Photos without GPS can inherit a location from their event, always labelled with confidence. |
| **Search** | "Ghat and Priya together in Goa in 2024", "best photos from last summer", "beach", "screenshots". Shows you exactly how it read the query. |
| **Duplicates** | Exact, resized, recompressed, edited, cropped and screenshot copies — plus burst shots kept separate. Nothing is ever deleted. |
| **Quality** | Sharpness, exposure, resolution and an aesthetic proxy, so "best photos of X" means something. |
| **Timeline / Map / Memories** | Year → month → event browsing, a map of everywhere you've been, and an "on this day" home screen. |

---

## Requirements

- Python 3.11+
- ~3 GB of disk for models, plus roughly 3 GB of thumbnail cache per 100k photos
  (measured at 28.5 kB/photo on a real 11 MP library; duplicates share one thumbnail)
- Optional: an NVIDIA GPU. Everything runs on CPU, just slower.
- Optional: Node 20+ if you want to rebuild the web UI from source

## Install

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere

# Install the PyTorch build that matches your GPU — see https://pytorch.org/get-started/locally/
# The cu126 pin below is not a general recommendation: it was needed for a Pascal-era card.
# CPU-only is simply: pip install torch torchvision
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126

pip install -r requirements.txt

python -m photointel models download     # face models (~280 MB)
python -m photointel geo-setup           # offline place names (~30 MB)
```

The semantic model (SigLIP2, ~800 MB) downloads automatically the first time it is used.

### Optional: richer place names

```bash
python -m photointel geo-setup --countries IN,US     # every village + landmarks for those countries
python -m photointel geo-setup --auto                # countries already present in your library
```

Without this you get cities and towns above ~500 inhabitants worldwide. With it you also get
small villages and landmarks (temples, forts, beaches, peaks, parks).

## Use

```bash
python -m photointel add-root "D:/Photos"
python -m photointel index        # scan + analyse; safe to interrupt and re-run
python -m photointel serve        # http://127.0.0.1:8765
```

Re-running `index` only processes what is new or changed.

| Command | |
|---|---|
| `index` | Scan roots and analyse new/changed photos, then rebuild people, events and duplicates |
| `index --post-only` | Just rebuild people/events/duplicates from existing analysis |
| `index --retry-errors` | Retry photos that previously failed |
| `caption --limit 500` | Describe photos with a local vision model (~2/s) |
| `status` | Library summary |
| `serve --port 8765` | Run the web app |

Everything a library owns lives in one data directory (`./data`, or `--data` / `$PHOTOINTEL_DATA`).
A few things are properties of the *machine* rather than the library, so a second library can
share them instead of downloading another copy:

| Variable | |
|---|---|
| `PHOTOINTEL_DATA` | the library's data directory |
| `PHOTOINTEL_MODELS` | model weights (~1 GB) — share across libraries |
| `PHOTOINTEL_GEO` | offline place-name data — share across libraries |
| `PHOTOINTEL_DEVICE` | `cuda` / `cpu` / `auto` for one run, without editing settings |
| `PHOTOINTEL_DEV=1` | allow the Vite dev server to call the API cross-origin (off by default) |

---

## How it works

### Indexing

A streaming pipeline: a feeder walks the roots, N worker threads read/decode/hash/thumbnail
each photo, one GPU stage runs the neural nets in batches, and a single writer commits in small
transactions.

- **Incremental** — a photo is re-analysed only if its size or mtime changed, or if the model
  that produced its data has changed.
- **Resumable** — progress is committed continuously; an interrupted run picks up where it left off.
- **Isolating** — a corrupt or unreadable file is recorded in `processing_errors` and the run continues.
- **Move-aware** — a file that reappears elsewhere with the same content keeps its identity, faces and people.
- **Read-only** — originals are opened for reading; only the cache directory is written.

Formats: JPEG, PNG, WebP, HEIC/HEIF, AVIF, TIFF, BMP, GIF, and RAW (CR2/CR3/NEF/ARW/DNG/ORF/RW2/RAF…)
via embedded previews where available.

**Google Takeout exports** are handled from the image itself, not from the `.json` sidecars
Takeout writes beside each photo. That is deliberate and measured: across 6,415 sidecars in a real
export, the date already derived from EXIF or the filename agreed with `photoTakenTime` 98.6% of
the time, and in the disagreements the sidecar was the *worse* answer — WhatsApp images dated
2018-09-14 by filename carried a Google timestamp of 2020-01-08 15:46, seconds apart across many
files, which records a bulk upload rather than a capture. Sidecars are therefore ignored.

### Faces and people

SCRFD-10G detection → 5-point alignment → ArcFace R50 (WebFace600K) embeddings → a kNN graph →
Chinese-Whispers label propagation → cluster merging → a stricter "attach" pass for weak faces.

Chinese Whispers is used rather than connected components because a single bad edge cannot chain
two identities together: a face only adopts the label with the strongest total support.

Low-quality faces (small, blurry, extreme pose, low feature norm) can never *create* a person —
they can only be attached to one afterwards, at a higher threshold.

**Your corrections win.** Assigning a face locks it. Rejecting a face records a permanent
(face, person) constraint. Splitting records "these are not the same person". Re-clustering
respects all of it, and named people keep their ids.

### Events

Photos are segmented by capture time (gap > 2.5 h), split further when GPS jumps > 30 km inside
45 minutes, then merged back when two sessions share a day and a *city* — comparing cities rather
than raw place ids, since a day out resolves to many different neighbourhood records.

Multi-day runs of events far from home become trips (home = where you spend the most distinct days).

Event titles come from the dominant category when the evidence is strong enough, otherwise the
place, otherwise a meaningful folder name. A wrong title is worse than a plain one, so a category
is only used when it clearly beats the runner-up.

### Semantics

SigLIP2 ViT-B/16 image embeddings power visual search, tags, and near-duplicate detection.

Tag scores are the *minimum* of two standardised views of the same similarity: how unusual the
score is for that tag across your library, and how much the tag stands out for that photo.
SigLIP's raw probabilities are useless as an absolute threshold (p99 ≈ 0.001 on COCO).

### Search

The query is matched against your library's own vocabulary — people, places, event titles, tags —
plus a date grammar ("last summer", "between 2019 and 2021", "August 2024"). Whatever is left
becomes the visual part of the query. Every decision comes back as a chip so you can see why you
got these results.

Enabling the optional Claude layer adds a fallback for queries the rules can't parse; it returns
the *same* structured filter, and names it invents that aren't in your library are discarded.

---

## Privacy

- Face recognition, clustering, search, tagging and captioning all run locally with open models.
- Reverse geocoding is offline — coordinates never leave the machine.
- Map tiles are **off** by default (the offline vector basemap is used instead), because
  requesting tiles tells a server which places you are looking at.
- The Claude layer is **off** by default. When on, it receives your query text and a vocabulary
  list — never photos, paths, EXIF or coordinates. Sending images is a separate switch, off by
  default, and applies only to photos you explicitly ask about.
- Original files are never modified, moved or deleted. "Hide" only affects your library views.

---

## Measured quality

Numbers from `eval/` on this machine. They describe these datasets, not your library.

**Faces — LFW** (13,185 detected faces, 5,749 identities):

| | |
|---|---|
| TAR @ FAR 1e-4 | 99.6% (threshold 0.269) |
| Best verification accuracy | 99.8% |
| Clustering: 60 recurring people among 800 strangers | 60 clusters found, BCubed F1 **0.997**, pairwise precision 0.998 |

The strangers stayed unclustered, which is the point: one-off faces must not become people.

**Everything else — a 1,490-photo synthetic library** with ground truth (`eval/make_test_library.py`
builds it from COCO scenes and LFW faces, with EXIF dates, GPS, duplicate families, bursts and a
folder of deliberately broken files):

| | |
|---|---|
| Indexed | 1,490 / 1,490; the one unreadable file isolated as an error |
| Capture date exact | 94.5% (the rest are files with no date at all, which correctly fall back to mtime) |
| Provenance (camera/phone/WhatsApp/screenshot/…) | 99.2% |
| People — photo-level precision / recall / F1 | **0.987 / 0.966 / 0.976** |
| Events — trip-level precision / recall | **1.00 / 1.00** (day-level BCubed F1 0.887) |
| Duplicates — pair precision / recall | **0.89 / 0.88**, with 78/90 burst pairs correctly classed "similar" rather than duplicate |
| City accuracy from GPS | 92.7% of 967 checked photos |
| Search — person / place / date / file-kind (7 queries) | macro precision **0.94**, recall **0.99**, p@20 0.91 |

**Visual search — COCO val2017** (32 natural-language queries against a library of 5,000
annotated photos plus 13,233 unlabelled distractors). The synthetic library above cannot measure
this: it composites faces onto unrelated COCO scenes, so a photo whose *event* is a wedding shows
a coffee cup, and any correct system scores near zero. COCO's human annotations describe the
actual pixels, so they can — instance labels for objects, captions for scenes:

| | precision@20 | macro P / R |
|---|---|---|
| **Object queries** — "a dog", "pizza", "someone playing tennis" (20, graded on instance labels) | **0.97** | 0.78 / 0.77 |
| **Scene & occasion queries** — "a wedding", "birthday party", "a kitchen", "city at night" (12, graded on captions) | **0.72** | 0.47 / 0.87 |

Median query latency 137 ms over 18,233 photos.

Scene scores are a **lower bound**, and noticeably so for the rarer ones. A caption-derived
ground truth only contains photos whose describers happened to use the word, so the top hits for
"animals at the zoo" — zebras and giraffes behind enclosure fencing — and for "birthday party" —
cakes with lit candles, children in party hats — are correct but score as misses. The keyword
lists were deliberately not widened to match whatever the engine returned, since that would
measure how well the ground truth had been fitted to the answer.

Reproduce:

```bash
python eval/make_test_library.py --out D:/pi_cache/testlib --clean
python -m photointel --data D:/pi_cache/testdata add-root D:/pi_cache/testlib
python -m photointel --data D:/pi_cache/testdata index
python eval/evaluate.py --data D:/pi_cache/testdata --library D:/pi_cache/testlib

# visual search, graded against COCO's own instance labels
python eval/eval_visual_search.py --data D:/pi_cache/scaledata
```

**Face clustering on a real library.** The synthetic and LFW benchmarks both said clustering was
near-perfect; a real 56,617-face library disagreed. There is no answer key for it, so quality is
measured as *cluster coherence* — a genuine identity has high mean intra-cluster cosine similarity
and almost no near-orthogonal pairs, while a chained cluster has a long tail of them:

| | before | after |
|---|---|---|
| largest "person" | 14,321 faces (25% of the library) | 6,212 faces |
| its mean intra-similarity | 0.26 (69% of pairs below 0.3) | 0.72 (2% below 0.3) |
| faces in incoherent clusters | ~25% | **2.2%** |
| faces assigned to someone | 48,885 | 51,904 (91.7%) |

Two defects caused it, both invisible at small scale:

- `merge_threshold` sat at 0.55, on a percolation cliff — at 0.52 the top cluster explodes to
  15,042 faces, at 0.68 the share of faces in incoherent clusters falls to 0.6%. It is now 0.68,
  chosen by sweeping that share, with LFW BCubed F1 unchanged (0.997 -> 0.996).
- A full recluster could never *repair* an over-merge: each new cluster was mapped back to
  whichever existing person held half its faces, so one bloated person reclaimed every cluster it
  had contributed to. A person id may now continue into only one cluster — the one holding most of
  their faces — and the rest become new people. Names and corrections still survive.

On **your own** library there is no answer key, so a different tool applies — it prints the
distributions and flags the failure modes that matter (a cluster that swallowed the library,
dates in the future, everything in one event):

```bash
python eval/inspect_library.py --data ./data
```

Also in `eval/`: `calibrate_faces.py` (LFW threshold sweeps), `calibrate_tags.py` (tag thresholds
against COCO instance annotations), `embed_lfw.py`, `scale_benchmark.py`.

### Honest limitations

- The synthetic library composites LFW faces onto COCO scenes. Real family photos — children
  growing up, side profiles, dim indoor light — are harder than this.
- Its "wedding" photos are COCO dining tables, so the wedding *category* and text search for
  "wedding" score poorly there. That is a property of the test data, not a measured claim about
  real weddings; `eval/evaluate.py` now marks those rows ungradable and excludes them from its
  averages instead of reporting a number that means nothing.
- Scene and occasion queries are measured against caption keywords, which undercount. Their
  true accuracy is somewhere above the reported 0.72 p@20, but how far above is not established.
- Event boundaries and duplicate thresholds are tuned, not learned. They are configurable.
- The aesthetic score is a zero-shot proxy, not a trained aesthetics model.
- Face thresholds were calibrated on LFW, which is adult celebrity portraits, then corrected
  against a real 56k-face library (see above). 2.2% of faces still sit in clusters that mix
  identities; the merge/split tools exist because no threshold gets this perfect.
- Cluster coherence is a proxy for correctness, not ground truth. Nobody has labelled who is
  actually who in that library, so "0.72 mean intra-similarity" says a cluster is internally
  consistent, not that it is the right person.
- **RAW decoding is unverified.** The code path exists (embedded preview, else demosaic) and its
  failure handling is tested, but no camera RAW file was available here, so no CR2/NEF/ARW/DNG
  has actually been decoded. HEIC, by contrast, is covered by tests and by 1,130 real files.

---

## Performance

Measured on a laptop with a GTX 1050 Ti (4 GB) and a 4-core i7-7700HQ.

**A real 18,233-photo library** (COCO val2017 + LFW, 17,041 faces found), indexed end to end:

| | |
|---|---|
| Full analysis (decode, hashes, EXIF, thumbnails, faces, embeddings) | **13.0 photos/s** sustained, 4 worker threads |
| Re-scan of all 18,233 files, nothing changed | **3.1 s**, zero photos re-processed |
| Face clustering, 17,041 faces | 16.6 s |
| Duplicate detection | 11.6 s |
| Event grouping | 7.4 s |
| Search index rebuild | 0.3 s |
| Face detection | ~18 ms/photo · recognition ~6 ms/face (batched) |

**A real 23,488-photo personal library** (302 GB of mixed phone, camera, WhatsApp and Google
Takeout folders; 22,161 JPG + 1,130 HEIC + others; 11.1 MP average, 2.60 faces per photo):

| | |
|---|---|
| Indexed | **23,488 / 23,488, zero failures** — including all 1,130 HEIC files |
| Full analysis | **~6 photos/s** (GPU-bound; the GPU sits at 100% while the CPU idles at 50%) |
| Faces found | 56,617 |
| Face clustering | 33.6 s · events 10.1 s · duplicates 33.7 s · tags 22.7 s |
| Byte-identical redundancy found | 25.8% of files (overlapping exports of the same photos) |
| Thumbnail cache | 28.5 kB/photo |
| Web API, warm | 36–208 ms per endpoint |
| Page load in the browser | ~2–4 s; the grid mounts ~50 rows, not 23,488 |

The 6 photos/s here against 13 photos/s on COCO/LFW is not a regression: real photos are 11.1 MP
with 2.60 faces each versus 0.1 MP with 0.93, so there is ~110x the decode work and ~2.8x the
face-embedding work per photo.

**A 100,000-photo / 150,000-face library** (`eval/scale_benchmark.py`, synthetic database, no
image files — this measures everything that happens *after* analysis):

| | |
|---|---|
| Face clustering + assignment | 103 s |
| Duplicate detection | 99 s |
| Event detection | 19 s |
| Vector search, one query over all photos | 0.01 s |
| Grid / timeline / person SQL queries | 0.01–0.30 s |
| Database size | 740 MB (7.4 kB per photo) |

At 13 photos/s a 100k-photo library takes about 2.1 hours of analysis, resumable at any point.
A modern GPU is several times faster. On CPU only, expect roughly 1–2 photos/s.

---

## Layout

```
photointel/
  config.py context.py db.py schema.sql     settings, app context, database
  imaging.py metadata.py hashing.py quality.py   decode, EXIF, hashes, quality
  geo.py geodata.py                         offline reverse geocoding
  vision/     faces.py semantic.py captioner.py vocab.py
  pipeline/   scanner.py indexer.py post.py jobs.py
  engine/     clustering.py people.py events.py places.py duplicates.py tags.py
  search/     parser.py engine.py llm.py
  api/        app.py routes_*.py images.py
web/          React + TypeScript UI
eval/         dataset builders, calibration, end-to-end evaluation
tests/        125 tests, no GPU required
```

The layering is deliberate: vision → features → database → relationship engines → search → UI.
The LLM sits at the top as a language interface only. It is never the database and never the
face recogniser.

## Tests

```bash
python -m pytest tests/ -q
```

They stub the neural nets, so all 125 tests run on CPU in about 70 seconds and still cover
scanning, incremental re-indexing, moves, decoding, metadata, clustering, corrections, events,
duplicates, search parsing and the HTTP API. The fixtures seed their randomness from stable
hashes, so a failure reproduces on the next run instead of disappearing.

## Licence

Source code is MIT (see `LICENSE`). The model weights are **not** covered by it — notably the
default face pipeline is non-commercial, so commercial use as shipped would require swapping it out.

### Model licences

- **InsightFace buffalo_l** (SCRFD + ArcFace) — non-commercial research use.
- **SigLIP2** via open_clip — Apache 2.0.
- **Florence-2** (captions) — MIT.
- **GeoNames** data — CC BY 4.0.
