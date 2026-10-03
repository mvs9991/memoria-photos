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

- **Originals are never modified, and never moved or deleted except by the user through the Trash.**
  Everything else written goes under one data directory; photos are opened read-only. (Changed on
  2026-09-26 at the owner's request: they wanted real deletes, but "no accidental deletes by AI or
  anything". See *The Trash is the only code that removes a file* in §4 for how that is enforced.)
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
  video.py                                     video metadata/frames, motion photos, transcoding
  engine/    clustering.py people.py events.py places.py duplicates.py tags.py
             live.py albums.py takeout.py ocr.py stacks.py corrections.py xmp.py
  search/    parser.py engine.py llm.py
  api/       app.py routes_*.py images.py
web/         React + TypeScript UI (Vite)
eval/        dataset builders, calibration, evaluation, library inspector
tests/       470 tests, no GPU required
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

**Takeout sidecars: each field is trusted for exactly one thing** (`engine/takeout.py`). The date only
replaces a file-modification-time date (in an unzipped Takeout that is the extraction day) — never EXIF
or a filename, per the measurement above. Location only fills photos with no GPS, and stays labelled
`takeout`/medium after geocoding (`geocode_photos` preserves it; a test proves the old code would not).
Favourite/trash apply on *first* import only, so a user's later change is never undone by a re-run.
People names are never written to a person: a sidecar says who is in a photo, not which face is whom.
A name is suggested only when it covers most of that person's labelled photos (share >= 0.6) *and* most
photos carrying the name show that person (coverage >= 0.5); the second test is what stops a friend who
appears beside Priya in every photo from being called Priya. Names and people are matched one-to-one.
Unmeasured on a real export — the thresholds are a starting point.

**Newer Takeouts name sidecars `IMG.jpg.supplemental-metadata.json`, truncated to fit 51 characters.**
GooglePhotosTakeoutHelper (read at its last commit, Jan 2025) has no rule for this. The matcher accepts a
truncated prefix only if it is >= 30 characters or runs past the media name, and a `(n)` copy only
matches a sidecar with the same `(n)`; see `test_sidecar_name_matching`.

**Albums show content, not files.** Takeout writes each album as byte-identical copies of year-folder
files, which the duplicate finder rightly calls exact duplicates. Membership is resolved through
`sha256`, so hiding either copy leaves the album intact.

**The motion half of a Live photo is not a photo.** `live_component = 1` keeps it out of listings,
search embeddings, face clustering (otherwise every Live photo is two sightings of each face) and
duplicates. A video is only ever an *exact* duplicate: its pHash and embedding describe one frame, and
without that rule a clip is a "near duplicate" of the still shot beside it (tests prove both).

**`META_VERSION` was not bumped for schema v2.** Bumping it re-analyses every photo (65 minutes for 23k
on the build machine). Motion-photo detection for already-indexed JPEGs is a post-stage backfill reading
256 KB per file; old Samsung files that declare the video only in a trailer are found on (re)index instead.

**Automatic tags rank, user tags filter.** A user tag is a hard filter. An automatic tag next to text to
find (`receipt that says invoice`) only ranks — a receipt the tagger missed must still be found, and a
screenshot is not excluded then. Removing an automatic tag writes a `user_removed` row that the tagger's
upsert will not overwrite.

**Where the v3 features came from, and what was left out.** They were chosen by reading the code of the
most-starred projects (Sept 2026): smart albums (lap, PhotoPrism), stacks (Immich, PhotoPrism, lap),
ratings and culling/compare (Immich, lap), birthdays (Immich), date/place edits (Immich, Nextcloud
Memories), XMP export (PhotoPrism, Immich). Left out on purpose: multilingual search (SigLIP2 may already
handle it — unmeasured, so not claimed), pet recognition (Ente's cat/dog models: licence and accuracy
unverified), and anything that writes to originals (Memories' Takeout migration writes EXIF into files;
lap and Damselfly move/delete them). Memoria's differences from those projects — query parsing, events/
trips, duplicate classes, quality ranking — are design differences; no head-to-head accuracy was measured.

**Corrections are overrides, not edits.** A user's date/place lives in `photo_overrides` and is re-applied
by the indexer after every metadata write (a test proves a changed file keeps its corrected date; without
the re-apply it reverts to EXIF). Clearing a correction sets `meta_version = NULL` so the file is re-read.
Geocoding keeps `location_source = 'user'` like it keeps `'takeout'`.

**Stacks fold only the timeline.** `stack_hidden` is honoured by `/photos/index?collapse_stacks=true`
(the Photos page) and nowhere else: search, people, places and albums still see every frame, and faces in
every frame still count. pHash alone could not separate a burst from a different scene shot a second
later (17 bits apart vs 12 for true burst frames on the test images), so the embedding must agree too —
a test fails without that check. A split stack is remembered by the hash of its members.

**A smart album is a search, so it is not album vocabulary.** "Save as smart album" names the album after
its query; left in the parser's album list, the name then captured its own query ("screenshots" →
photos in album Screenshots → none). Found in a browser run, fixed, and pinned by a test that fails on
the old code.

**Overlays opened from a sticky bar are portalled to `<body>`.** `backdrop-filter` on `.review-bar`
makes it the containing block for `position: fixed` children, which squashed the compare view into the
bar. `components/Portal.tsx` is the fix; use it for any new modal opened from inside a bar.

**A password guards `/api`, not the pages.** The middleware in `api/app.py` answers 401 for every
`/api` route except `/api/auth/*` and `/api/share/*` when a password is set; the React login screen is
only presentation. Sessions are HMAC-signed with `data/secret.key` and last 30 days; changing the
password writes `data/password.changed`, which invalidates every older session. `serve` refuses a
non-loopback host without a password (`--insecure` overrides). Tests fail without the middleware and
without the refusal.

**A share link reaches exactly one album.** Every `/api/share/{token}/…` route re-checks that the photo
belongs to the token's album (smart albums re-run their search), strips the owner's favourite flag,
and serves downloads only if the link allows them. A test fails without the membership check. Tokens
are 128-bit, stored in `share_links`, and die with the album or on revoke.

**GPS tracks never override a better location.** Only photos with no GPS, a high/medium date, and no
Takeout or user location are placed; the result is `location_source = 'gpx'`, medium confidence, and
the geocoder keeps that label. Both neighbouring track points must be within 5 minutes of the photo.
A test fails without the `gps_lat IS NULL` rule.

**Clean-up lists only hide.** `POST /photos/hide` sets `hidden`; the Hidden collection
(`collection=hidden` flips the visibility filter) is the way back. Nothing on the Collections page
touches a file, and the thresholds (blur < 35, ≥ 20 MB) are heuristics, not measured.

**The Trash is the only code that removes a file.** Deleting renames the file (plus its Live video and
`<name>.*.json/.xmp` sidecars) into `<data>/trash/<id>/`, or `<root>/.memoria-trash/<id>/` when the photos
are on another drive (a dot-directory, so the scanner skips it). Rename only — never copy-then-delete —
so it needs no space and cannot half-lose a file; the plan is committed before any rename and
`trash.reconcile()` undoes an interrupted one at start-up. Status goes to `trashed` (hidden everywhere,
because every view filters `status = 'ok'`), then `deleted` once erased; the scanner leaves both alone.
Restore never overwrites: a taken name gets ` (restored)`. Erasing happens only for entries older than
`trash_days` (a sweep at start-up and every 6 h) or when the user empties the trash or deletes
permanently. Guards: every destructive API call carries `confirm` = the count shown in the dialog
(mismatch → 400); the UI asks for the number to be typed from 25 files up, and always for permanent
deletes; `allow_delete = false` removes every Delete button and the server refuses. Two tests pin it:
`test_only_the_trash_can_remove_a_file` lists every removal call in the package (a new one fails until
reviewed) and `test_only_a_users_click_can_trash` allows `move_to_trash` only in `api/routes_trash.py`.
All 14 guards were mutation-checked: each test fails without its guard.

Building it exposed a latent bug: `update_person_stats(conn, person_ids)` had never been called with ids
and produced `WHERE … WHERE`. Fixed; `test_person_counts_follow_the_trash` failed before the fix.

**Access is decided in one place.** `api/app.py`'s middleware resolves the account (signed cookie,
millisecond issue time compared with that account's `pw_changed_at`, so a password change ends its
sessions even within the same second), then `accounts.allowed(role, method, path)` — an allowlist by
path prefix, owner-only for anything that deletes, changes settings, writes to the server's disks or
opens the Locked folder. Tests in `tests/test_v6_access.py` were mutation-checked. Without accounts the
old single password works as before; with neither, everyone is the owner (loopback only by default).

**Locked photos have their own status.** `status = 'locked'` keeps them out of every view that lists
`'ok'` photos without touching those queries; `deps.guard_locked` makes every endpoint that serves a
photo's pixels, details, video or face crop answer 404 unless an owner opened the folder in that
browser (15-minute token; a PIN change closes it everywhere). The indexer, scanner and moved-file
logic keep `locked` through re-analysis. Two layers enforce owner-only opening (middleware and guard);
a test covers the shared-tablet case where a family member signs in on the owner's open browser.

**Uploads, edits and creations are the only files Memoria writes into a root**, and only into the
upload folder (`Settings.upload_folder`, default `<data>/uploads`, registered as a root). Each is
written to a unique temp file (`mkstemp`; a clock-based name collided on Windows and mixed two photos'
bytes — found in a demo run by hash check) and renamed onto a name claimed with exclusive create, so
nothing is ever overwritten. Duplicates by sha256 against the library and the `uploads` log are
skipped; a shared-album upload that is a duplicate still joins the album.

**Backup is copy-only.** `engine/backup.py` walks every root (skipping `.memoria-trash` and
`.incoming`), copies new/changed files through `.part` with a hash computed while reading and compared
with the indexed sha256, never replaces a newer backup copy with an older file, and never deletes. The
database is snapshotted with SQLite's backup API. The scheduler (`photointel/scheduler.py`) runs it
weekly once a folder is set, and looks for new photos hourly; `due()` is pure and tested.

**Guessing is locked out** (`photointel/ratelimit.py`): 10 wrong logins in 15 minutes per device
and per account name, or 5 wrong Locked-folder PINs across all devices, shut that door for 15
minutes — even to the right answer. In memory per process; a restart clears it.

**A deep review (2026-09-26/27) found 16 more bugs**, all fixed with regression tests in
`tests/test_review_fixes.py` that failed first: an old cookie signing in as a re-used account id;
exports into a root's parent writing inside the library; non-owners listing server folders, publishing
share links and seeing library paths in settings; locked photos leaking through describe/similar;
backup/movie jobs swallowing index requests; a failed backup retried every minute; stack covers
leaving view taking their stacks with them, and chosen covers lost on rebuild; a rotated-thumbnail
temp race; albums over 900 photos; event covers that had left view; unlimited password/PIN guessing.
Known and left: thumbnails are cached for good, so places that show a photo without its rotation in
the URL (covers, slideshow, compare) can show it unturned until the browser cache clears.

**Export only copies.** `engine/export.py` resolves a spec (ids, album, event, year/month, people in
`each`/`together`/`any` mode) to files, copies with `shutil.copy2` into a `.part` then renames, and skips
a destination that already holds the same bytes (sha256), so a re-run resumes and identical copies of
one photo are written once per folder. A destination inside a root is refused. The zip streams through an
unseekable sink (data descriptors, ZIP_STORED, zip64 as needed) so nothing is buffered on disk. Folder
exports run as `kind='export'` jobs in a server thread; `spawn_index_job` ignores export jobs when
deduplicating, or a running export would swallow an index request (a test covers it).

**`.nomedia` is deliberately not honoured.** WhatsApp puts it in `Sent`, which held 1,339 of the
owner's own photos. Dot-directories are already excluded, which handles real app-cache junk.

**Reclaimable bytes exclude `similar` groups.** Those are different photographs that merely look
alike; deleting them reclaims nothing you had twice.

---

**v7: phone backup, other apps' XMP, per-person favourites, offline (Sept 2026).**

- *WebDAV is a drop box, not a file server.* `/dav/` lists only what that account sent through it,
  every write goes through the same `save_upload` as the Upload page (dedupe by sha256, filed by date),
  and DELETE is refused: a backup app that "mirrors" deletions must never be able to remove a photo.
  Files a backup app parks under a temporary name are held in `dav_pending` until the MOVE to the real
  name. Indexing waits for 60 s without uploads (`scheduler.uploads_due`) so a 2,000-photo first sync
  is one index run, not 2,000.
- *XMP import never overrides the user.* A sidecar's rating and description apply only where Memoria has
  none; its keywords become tags only if they are new since that sidecar was last read (so removing a
  tag here sticks); names only feed the same suggestion list as Takeout. Memoria's own exports carry
  `memoria` as the creator tool and are skipped, or export → import would loop.
- *Favourites become per person when accounts are turned on*: `photos.favorite` is handed to the owner
  (`favorites.hand_to`) and stays the library's flag without accounts. An import's favourite goes to
  every owner. `favorites.expr(alias, uid)` is the one SQL fragment every query uses — a place that
  reads `p.favorite` directly is a bug.
- *A private album is invisible, not forbidden*: `albums.can_see` returns nothing, so the API answers
  404, like a locked photo. Only the account that made an album can make it private; imported and
  pre-accounts albums have no maker and stay shared. Search drops private album names from the
  vocabulary for everyone else, so an album name cannot be probed through search.
- *Offline caching is decided by the server's headers, not a list of routes.* The service worker keeps
  an image only when it is `immutable` and never when `no-store`; `guard_locked` marks every response
  that shows a locked photo `no-store` (through a dict in a contextvar — sync routes run on a copied
  context, so a plain flag set there never reaches the middleware). Images changed from `public` to
  `private`. The worker's never-list (auth, share, locked, accounts, originals, downloads, video, dav)
  is checked by a test that parses `sw.js`. `index.html`, `sw.js` and `manifest.json` are served
  `no-cache` so a phone picks up a new build.

**v8: running unattended (Sept 2026).**

- *The keeper restarts on crash, never on refusal.* `service.decide`: exit 0 stops, exit 2 (a
  configuration refusal such as serving to the network without a password) stops for good — retrying
  would fail forever and hide the reason — exit 75 is the server asking for a restart, anything else is a
  crash, retried after 3, 10, 30, 60, then 300 s (reset after 10 healthy minutes). Because a refusal
  stops the keeper, the settings API refuses `serve_host = 0.0.0.0` without a password: otherwise one
  click would leave Memoria off after its next restart.
- *Health is judgement, labelled as such.* The thresholds (2 GB / 20 GB and 10 %, a phone quiet for 7
  days, a backup late at 1.5× its interval, 3 crashes a day) are not measured. "Quiet phone" uses the
  time a backup app last *connected* (`meta dav_seen:<name>`), not the last upload: a phone with no new
  photos is not a problem. Webhook alerts go out again after a day for errors and a week for warnings,
  and again as new if a problem is fixed and comes back.
- *The off-site copy names each folder relatively.* Given full paths, restic records every ancestor
  (C:\, C:\Users…) with its Windows security descriptor, and a restore recreates it: the first real
  restore produced a `C\Users` folder its own user could not write into or delete. Each root is now
  backed up from its parent by name (one snapshot per root, plus `memoria` for the database), and a
  restore gives `<target>/<folder name>/…`. `test_each_folder_is_copied_by_its_own_name` fails against
  the old behaviour. Snapshots are never pruned (copy-only, like the local backup), restores go only to
  an empty folder outside the roots and the data directory, with `--verify`.
- *The restic password lives in `<data>/offsite/`* so copies run unattended; the UI shows a generated one
  once and insists it is written down elsewhere — the data directory burns with the house.

**Private photos (v8).** A private photo has status `'private'` and `private_to` = its account, so the 75
queries that list visible photos (`status = 'ok'`) and every shared aggregate (people, events, the search
matrix, counts) leave it out without being touched — the Locked folder's mechanism, per person. What had
to change is every place a status is *decided* (scanner restore, indexer move and re-analysis): private
comes first there, before locked, so a private photo can never land in the owner's Locked folder.
`deps.guard_locked` refuses its pixels and details to anyone but its person, the owner included.
Ownership follows the upload folder `<upload root>/<name>/` rather than the uploads log, which is keyed by
content and names only the last sender of those bytes; new files there are claimed right after the scan,
before analysis, so they are never visible. Upload de-duplication ignores other people's private copies
(else a family member's own copy of the same photo would be swallowed). `test_nothing_the_owner_can_open_
mentions_a_private_photo` sweeps every list endpoint for the photo's name, fingerprint and folder; it and
the rest of `tests/test_private.py` were each checked to fail with the matching protection removed.

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

### Bugs found by sweeping the API rather than by using it

`tests/test_api_fuzz.py` and `tests/test_api_fuzz_writes.py` read every endpoint out of the
OpenAPI schema and push hostile values through the parameters each one declares, so an endpoint
added later is covered without anyone extending a list. Four defects, none of which any amount of
clicking around would have found:

1. **An integer beyond 64 bits was a 500 on about thirty-five read endpoints.** FastAPI validates
   `int` with Python's arbitrary-precision ints, so a 21-digit id passes the signature and only
   fails when SQLite is asked to bind it. Handled centrally as a 422.
2. **A year or month outside what `datetime()` accepts did the same.** Seven endpoints share
   `photo_filter_sql`, so the bounds live there. `year=0` deliberately stays a 200 — it is falsy,
   so it means "no filter", and that is pinned by its own test.
3. **The SPA catch-all answered unknown `/api/` paths with 200 and `index.html`**, so a caller
   expecting JSON got `Unexpected token <` instead of a 404.
4. **An unwritable export folder was a 500 and a stack trace** — `{"folder": "C:/Windows"}` got as
   far as trying to write `C:\Windows\memoria-export.json`. `_check_destination` now creates the
   destination up front so an impossible one is refused before any work starts, and the five
   export entry points turn an `OSError` into a 400 that names the reason.

   Worth knowing why it is not done with a write-probe file, which would be the obvious fix: a
   probe has to be deleted afterwards, and `test_only_the_trash_can_remove_a_file` scans the whole
   package for removal calls and fails on any that is not registered. That gate caught the probe
   on the first full run. It is a static source scan, so it is cheap and absolute — if you need a
   new `unlink`/`rmtree`/`rename` anywhere in `photointel/`, you must add it to `ALLOWED_REMOVALS`
   with a justification, which is the review step it exists to force.

The write sweep also asserts the project's hardest guarantee across the whole write surface at
once: after roughly 1,300 malformed requests to every mutating endpoint, every file under the
library root is still present and byte-for-byte identical.

Two more were found on the Linux CI job that Windows could not reproduce. A path longer than the
filesystem allows makes `is_dir()`/`is_file()` **raise** `ENAMETOOLONG` rather than answer False,
so a long URL was a 500 from `/api/browse`, from `/api/roots`, and from the SPA catch-all. Windows
does not raise, so all three looked fine locally. They are now simulated with a monkeypatched
`Path.is_dir` so either platform catches a regression, and the awkward-filename list gained the
names that are legal on Linux but impossible on Windows (`con.jpg`, `star*.jpg`, a trailing space)
which skip here and run there. **If you only ever run the suite on one OS, you are testing half of
it** — and a Windows run will not tell you the Docker image is broken.

### The leaked job subprocess, which looks exactly like flaky tests

Worth knowing before you chase a phantom. Two consecutive full runs failed on two different,
innocent tests: one with a numpy allocation failure, one with `fork: Resource temporarily
unavailable`. Both passed in isolation, which is the classic shape of a flaky test.

Neither was flaky. `jobs._spawn` starts a `DETACHED_PROCESS` on Windows so a production index run
outlives the server that started it — correct for production. In a test it leaves a real python
process pointed at a pytest tmp directory, which nothing reaps. One had been left behind by each
full suite run, and three of them were holding about 3 GB; the memory pressure then broke whatever
test happened to run next. Several tests already monkeypatched `_spawn`, so the hazard was known,
but the upload path did not. There is now an autouse `_no_detached_jobs` fixture in `conftest.py`
so no test can leak one, whatever it calls.

The lesson generalises: when a long suite fails on a different test each run and each passes alone,
suspect the host before the tests. `Get-Process python` is the first thing to look at.

Two notes on reading these tests. A 5xx is not automatically a bug — asking for off-site snapshots
when restic is not installed is a 502 with a sentence explaining why, which is a correct answer.
The sweep tells them apart by the `"error"` key that only the unhandled-exception handler adds.
And `/api/export/xmp` writing wherever it is pointed is the feature, not a hole: it is owner-only,
and the owner choosing an export folder is the whole point. What it may never do is write inside a
photo root, which `refuse_inside_roots` enforces and a test pins next to the unwritable-folder one,
so a later change cannot trade one for the other.

### "Clicking a person does nothing" was an 82-second query

Found on the 29,514-photo library, reported by the user as a UI bug. The People page opened a
person instantly and then showed a blank photo area for well over a minute, which reads as a click
that did nothing. `/api/people/6` answered in 0.1 s; `/api/photos/index?person=6` took **82 s**.

`photo_filter_sql` asks "does this photo contain person N?" with `EXISTS (SELECT 1 FROM faces ...
WHERE photo_id = p.id AND person_id = ?)`. With only single-column indexes, SQLite chose
`ix_faces_person` and, for every one of ~29,000 photos, walked all of that person's 6,729 faces
looking for a match: around 100 million index steps. It prefers that index because without
`ANALYZE` it assumes a person has few faces, which is wrong for exactly the people you open most.

Fix: one composite index, `ix_faces_person_photo (person_id, photo_id)`, in `schema.sql`, which is
re-run on every start so existing libraries pick it up on the next restart. 82 s became 0.2 s with
byte-identical output. The same `EXISTS` pattern is used in six places (photo filter, events,
export, search), so fixing the index fixed them all, where rewriting each query would have left the
next one to be written wrong. `tests/test_person_filter_plan.py` pins the query *plan* rather than
a timing, because a test library is far too small to be slow and a timing assertion would be flaky.

The lesson, again: the unit tests and a 1,500-photo library cannot show this. Open the biggest
person on the biggest library before believing a page is fast.

### Auditing the library's *data*, not just its speed

After the 82-second query, the next sweep checked whether what the library says is true. Three
more defects, all invisible to a small test library:

1. **An Android phone's own trash was indexed as ordinary photos.** Deleting in Android's gallery
   renames a file `.trashed-<expiry>-<name>` and keeps it ~30 days; a backup of `DCIM` carries
   them along. 94 of them (75 photos, 19 videos, none present under a normal name) were among the
   newest items in the timeline. They are now **hidden, not skipped**: the scanner inserts them
   with `hidden = 1` (`config.PHONE_TRASH_PREFIX`) and a one-time step in `db.migrate` hides the
   ones an earlier version had indexed. Skipping was rejected on purpose, because an unseen file
   flips to `missing` (which the app reads as an unplugged drive) and anything still wanted would
   be lost; hidden ones are one "Show again" away in Collections → Hidden, and the step is
   recorded in `meta` (`phone_trash_hidden`) so showing one again is never undone. `.pending-` is
   deliberately *not* treated this way: it can be the only copy of a capture that never finished
   renaming. Google Takeout's `Bin/` was already hidden by the Takeout import (the other 30
   hidden items on that library).
2. **Duplicate groups could keep a hidden photo and mark the visible one redundant.** Detection
   looked at every photo, hidden or not: 16 groups had a hidden keeper and 11 had no visible
   member at all. It now considers only visible photos (`hidden = 0`). An empty result still
   leaves earlier groups alone, because that is also what an unplugged drive looks like and it
   must not erase review decisions.
3. **Every scheduled index ran all 17 post-processing stages, about 11 minutes, even when nothing
   had changed** (`takeout` 135–185 s, `icloud` ~100 s, `xmp` ~130 s, `gpx` ~129 s). The scheduler
   runs one hourly, and one a minute after the server starts if it is overdue. During it the
   database write lock was held for stretches longer than the 60 s busy timeout, so the
   scheduler's own check failed with "database is locked" (and an edit in the web app would have
   too). A scheduled run now passes `--if-changed` and skips post-processing when the scan found
   nothing new/changed/missing/restored and no photo needed analysis. Manual runs and explicit
   `--post-only` runs are never skipped. A run killed *during* post-processing leaves
   `meta.post_pending = 1`, so the next scheduled run finishes the job instead of trusting
   half-built people and events.

5. **A library could silently stop getting place names.** When the offline place data is not found
   the geocoding stage logs one warning and skips, so photos keep arriving with GPS and no place
   and nothing in the app says so. 697 were added to the real library that way (the data was on
   the machine in `data/geo`; the library's own `geo/` was empty and `PHOTOINTEL_GEO` was not set
   for the run). `health.check` now raises `geo:missing` ("Place names are switched off", with the
   count, the path it looked in, and the fix) when the data is absent *and* some photo has GPS but
   no place; it stays quiet when nothing needs a place. **Anything that runs `index` or `serve`
   must be started with the same `PHOTOINTEL_DATA`, `PHOTOINTEL_MODELS` and `PHOTOINTEL_GEO`**: a
   scheduled job is a child process and inherits the server's environment, so a server started
   without them silently runs every job without them. `geo-setup` downloads the data and is
   opt-in, like every other download here.

   Also added: `tests/test_no_lock_leaks.py` asserts that after *every* GET and every mutating
   endpoint (including with hostile bodies) a second connection can take the write lock at once.
   It matters because the API keeps one connection per worker thread for the life of the server,
   so a handler that writes without committing would hold the lock until that thread next
   commits, which could be much later. One test plants a deliberately leaky endpoint to prove the
   audit can see a leak. None of the real endpoints leak.

6. **Hidden photos leaked into everything derived from photos.** After 124 photos were hidden
   (a phone's and Google's trash) the real library had 24 events with no visible photo (empty
   ghosts in the Events list), 10 events using a hidden photo as their cover, 36 with a wrong
   count, 5 people listed with a count but no visible photo and 5 whose cover face came from a
   hidden photo. `detect_events` and `update_person_stats` filtered on `status` and never on
   `hidden`; and `visibility.refresh`, which the Hide button calls precisely to refresh counts,
   therefore changed nothing. Both now exclude hidden photos (`_persist_events` clears every
   photo's event first, so leaving them out of the load is all it takes). After rebuilding the
   real library: 0 ghost events, 0 wrong counts, 0 hidden covers, 0 ghost people.

   `visibility.refresh` now also sets `meta.post_pending = 1`. **That matters because of fix 3:**
   hiding, trashing, locking or making photos private changes no file on disk, so the scheduled
   run's scan cannot see it, and a "nothing changed" skip would never rebuild events, duplicates
   or stacks. The flag makes the next scheduled run do the full pass. Date and place corrections
   already start their own rebuild job.

   Method note for audits like this one: derived tables are checked against a *definition of
   visible* (`status='ok' AND hidden=0 AND live_component=0`). Trips link photos through
   `trip_photos`, not `photos.event_id`, so a naive "events with no photos" query reports every
   trip as a ghost; check `kind` separately.

What was checked and found sound, so nobody re-checks it: every `exact` duplicate group is
byte-identical (one distinct sha256 across 5,208 groups), no photo is in two exact groups, none
is missed, every keeper is a member, and member counts match. Every read endpoint answers in
under 3 s on the real library (slowest: `/api/health`, 2.8 s).

4. **Four post-processing stages that do almost nothing took 100-260 s each inside a job.** Profiled
   on a copy of the database, the stages cost 20 s (`takeout`), 0.2 s (`icloud`), 0.1 s (`xmp`) and
   0.1 s (`gpx`) on their own, yet 135, 99, 129 and 129 s in a real run. The cause was a
   self-deadlock, not work: a job runs its stages on one connection and reports progress through a
   second one (`JobReporter`). `import_takeout` ended with `db.bump_generation(...)` *after* its
   final `commit()`, so the connection finished the stage holding an uncommitted write; the next
   progress report, made by the same thread, then waited the full 60 s busy timeout for a lock that
   thread was holding (twice, because the heartbeat thread was queued on the same lock). Three other
   stages did the same. It also explains the hourly "database is locked" errors from the scheduler.
   Fixes, each with a test that fails without it: `run_post_stages` now commits after every stage
   and **rolls back** a stage that raised (its half-done writes used to be swept into the next
   stage's commit), `import_takeout` commits its last write, and the reporter's connection uses a
   5 s busy timeout, because progress and heartbeats are best-effort and must never be able to stall
   the caller for a minute. `tests/test_post_transactions.py` asserts the connection is idle at
   every progress report, which is the invariant that matters. Reproduced with
   `conn.in_transaction` after each stage, which is the quickest way to find the next leaker.

   **Method note.** Time a stage on its own and then inside the real job. A stage that is fast
   alone and slow in the job is waiting on something, not computing.

### Selecting photos and people (press-and-hold, drag)

`web/src/lib/dragSelect.ts` is shared by the photo grid and the People grid, and `useGridSelect` in
`SelectionBar.tsx` is what a page uses to get all of it (Select button, hold, drag, Ctrl+A, Esc).
Behaviour worth knowing before you change it:

- **Touch:** holding still for 450 ms starts selection; moving before that is a scroll and the
  gesture is dropped. After the hold, `touchmove` is blocked so the page does not scroll under the
  finger. **Mouse:** the same hold, or, once already selecting, a few pixels of movement starts a
  drag at once.
- The range is by **index into the item list**, not by which tiles are mounted, because the grid is
  virtualised and most tiles of a big library are not in the DOM. It is applied against a snapshot
  of the selection taken when the drag began, so dragging back shrinks the range.
- Starting a drag on an **already-selected** tile drags to *deselect*. That is deliberate (Apple
  Photos does it) and it surprised the test written for it first.
- A tile is only ever looked up **inside the grid the gesture started in**. The People page has two
  grids (Named, Discovered) that each number tiles from 0; without that scoping, dragging over the
  other grid applied its index to the wrong list. Found by a browser test, not by reading.
- The click that follows a hold or drag is swallowed for ~80 ms so releasing does not also open the
  photo or navigate to the person.
- `.content` has `scroll-behavior: smooth`, so edge auto-scroll uses `behavior: "instant"`; smooth
  scrolling turns every frame's nudge into an animation that fights the next.
- Esc leaves selection **unless** a `[role="dialog"]` or the slideshow is open, so cancelling the
  delete confirmation does not also discard the selection.
- **"Selecting" means the page's Select mode is on OR anything is selected.** `PhotoGrid` derives it
  (`selectMode || selection.size > 0`) rather than trusting the page's flag. Picking a photo with
  its check circle or Ctrl-click used to leave select mode off, so the *next plain click opened
  the preview* instead of adding to the selection. Those paths also call `onBeginSelect` so the
  page's own Select/Done button stays in step.
- **Select all, three levels:** a **Select all N** button beside Select on every page
  (`SelectToggle`, Ctrl+A too), a **Select all** control on every day/month section header
  (toggles that section, shows "Select all (3 of 25)" when partly picked, adds to what is already
  selected), and on People a Select all in the bar plus one per Named/Discovered section.
- **Merging more than two people asks first** (`window.confirm`). Select all makes it one click
  from "fold two thousand different people into one"; two is an ordinary merge.
- **The grid must not jump when the selection bar appears.** The bar is in flow, so the first
  pick pushed everything down ~110 px and the photo under the finger slid away mid-drag. Two
  guards: select mode shows a slim bar immediately (`active` prop, so there is usually nothing
  left to appear), and `dragSelect.ts` scrolls by however far the pressed tile moved for the
  first few frames after the hold (`keepTileUnderFinger`). A browser check asserts the pressed
  tile's top is unchanged.
- On the People page, **Hide** is one request (`POST /api/people/hide`) and reversible: it flips
  `persons.hidden` only and the page offers Undo. It never deletes a photo, and people have no
  delete action on purpose.

`web/jkgesture.mjs` (gitignored scratch) drove all of this with real mouse and touch input through
Playwright; 33 checks. It is worth recreating if you touch the gesture code, because none of it can
be exercised by the Python tests.

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
- **Video on real footage** is untested (only generated files): portrait iPhone rotation, HEVC transcode
  speed on long clips (transcoding is synchronous on first play), Android `creation_time` placeholders.
- **OCR drops spaces** on some text ("RELIANCEFRESH"); a quoted multi-word search can miss. Unmeasured.
- **Takeout name suggestions** are unmeasured on a real export (see §4).
- One `net::ERR_CONTENT_LENGTH_MISMATCH` was logged once in a browser run against the demo library and
  could not be reproduced in five further runs (cold and warm). Unexplained; watch for it with video.
- **GPX placement** is tested on generated tracks only. Photo time → UTC uses the EXIF offset, else this
  machine's zone on that date; a camera set to another zone without an offset tag lands wrongly.
- **Clean-up thresholds** (blur < 35, ≥ 20 MB, tagger's meme/document) are unmeasured heuristics.
- **Trash across drives:** a photo on a different drive from the data directory is trashed into
  `<root>/.memoria-trash`. Tested with the path forced, not on a real second drive; a read-only or
  network root makes the rename fail, and the file is then reported as not moved (never forced).
- **iCloud import** is tested on a constructed export only (CSV layout from Apple's description).
- **Rotation** is Memoria-only: exports copy the original bytes, so another app shows the photo
  unturned unless the user saves an edited copy (Edit → rotate → Save as a copy).
- **Colour thresholds** (12 % / 30 %) and the Pets collection (tagger's dog/cat) are unmeasured.
- **Phone upload from iOS Safari** is untested; Safari may convert HEIC to JPEG on upload.
- Found and fixed in v6 browser runs and tests: re-indexing after a metadata upgrade turned trashed
  photos into errors; two concurrent uploads could register the upload folder twice (UNIQUE error)
  or share a temp file; `update_person_stats(person_ids)` produced invalid SQL (never called with ids
  before); a fixed-aspect crop chosen before a turned preview arrived was not square.
- **Zip downloads over 4 GB** use zip64 and are untested at that size; the UI suggests copying to a
  folder instead.
- **Auth has had no outside security review.** It is PBKDF2 (240k rounds) + an HMAC session cookie,
  with a guessing lockout (10 wrong passwords per 15 min per address and per name, 5 wrong PINs) and
  HTTPS only with your own certificate. Fine behind a home router or a private VPN; do not forward a
  port to it.
- **WebDAV has been tested with a test client only**, not with a real FolderSync or PhotoSync. The
  methods they are documented to use (PROPFIND, MKCOL, PUT, MOVE) are implemented; COPY, LOCK and
  PROPPATCH answer 501. If an app insists on LOCK, it will need a no-op lock.
- **XMP import has been tested on hand-written sidecars** in the shapes Lightroom, digiKam and
  darktable document, not on files those apps actually wrote.
- **Tailscale HTTPS is untested with the real Tailscale** (none on the build machine): the flow runs
  against a stand-in program printing `tailscale status --json` in its documented shape and copying a
  certificate. HTTP and HTTPS served together were checked for real with an openssl-made certificate.
- **Auto-start** was tested by writing into a temporary Startup folder, never the real one; `--at-boot`
  (schtasks as SYSTEM) and the systemd/launchd entries are checked as text only, not run. SYSTEM does
  not see mapped network drives. The keeper itself was run for real: a killed server came back in
  seconds, and a restart asked for by the app was not counted as a crash.
- **The off-site copy on Linux/macOS, and to sftp/rest targets,** is untested; on Windows it was run
  end to end with restic 0.19.1 (copy, re-copy, sampled check, verified restore).
- **Private photos have one known limit:** a private photo is left out of the shared people, events,
  map and search index for its own person too (they see it only on their Private page). Building a
  second, per-person set of those was judged not worth the risk of leaking one set into the other.
- **The offline copy** was checked in Edge (Chromium) with the network cut: grid, viewer preview and
  opened pages load, the banner shows, sign-out wipes it, locking removes a photo from it. Not tried
  on iOS Safari, which limits service-worker storage and may evict it after weeks unused.
- Observed in the v4 demo: a Takeout album copy and its year-folder twin (same pixels, same second) are
  folded into one *stack* as well as grouped as duplicates. Harmless — the copy leaves the timeline —
  but the stack is a burst of one photo. Not changed; worth a rule if it confuses anyone.
- Cosmetic: folder tokens of ≤3 letters are uppercased as probable airport/city codes (BLR, HYD),
  so `Goa Trip 2019` titles as `GOA Trip`. Changing it risks breaking real codes.

---

## 10. Working notes

- Run the suite with `.venv/Scripts/python.exe -m pytest -q`. 470 tests, ~9 min, no GPU needed —
  the neural nets are replaced by deterministic fakes.
- Test fixtures seed randomness from `zlib.crc32` of the **file name**, not `hash()` (salted per
  process) and not the full path (contains pytest's per-run tmp counter). Both made failures
  irreproducible.
- The UI review tool is `web/shots.mjs` — it screenshots every screen, fails on console errors, any
  failed API request, and now asserts the grid is virtualising. Run it after UI changes:
  `node shots.mjs <outdir> http://127.0.0.1:8765` (add `PW_CHANNEL=msedge` when Playwright's own
  Chromium is not downloaded — its CDN timed out on the build machine). It waits for images to decode and for animations
  to settle, because an early capture once produced blank tiles and a half-counted "279 photos" on a
  1,489-photo library that looked exactly like a rendering bug.
- `npm run build` in `web/` rebuilds the SPA into `web/dist`, which the server serves.
- Long index runs should be started as a background task directly, not as `cmd & sleep; tail` — the
  child dies with the shell.
- Indexing is resumable and safe to interrupt; `index` re-processes only what changed. An
  `index.lock` in the data directory prevents two concurrent runs (it takes over a lock whose pid is
  gone).
