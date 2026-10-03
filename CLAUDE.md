# Memoria — local photo intelligence

Repo: https://github.com/mvs9991/memoria-photos · setup from a clone is `HANDOFF.md` §3.

Indexes a folder of photos and works out who is in them, when and where they were taken, what they
show, which are duplicates, and how they group into events and trips. Serves a React photo app with
natural-language search. Python 3.11 + PyTorch + SQLite + FastAPI; everything runs locally.

**Read `HANDOFF.md` before changing anything in `photointel/engine/`, the clustering thresholds, or
the evaluation scripts.** It records why the current values were chosen, what was measured, what was
tried and rejected, and what is still unverified. `README.md` documents the product itself.

## Non-negotiables

- **Never modify an original photo, and never move or delete one except through the Trash on the
  user's explicit click** (`engine/trash.py`, `api/routes_trash.py` — see `HANDOFF.md` §4). No
  automatic code path may remove a file; `test_only_the_trash_can_remove_a_file` enforces it. All other
  output goes under one data directory; originals are opened read-only. Exports only ever copy.
- **No LLM in the recognition path.** Face identity is SCRFD + ArcFace + graph clustering. The
  optional Claude layer only parses queries the rule parser cannot, and returns the same structured
  filter it would have.
- **Local by default.** Offline geocoding; map tiles, captions and the Claude layer are opt-in.
- **Never state an accuracy number that has not been measured**, and name the dataset it came from.
  Where something cannot be measured, say so. See `HANDOFF.md` §5–6.

## Commands

```bash
.venv/Scripts/python.exe -m pytest -q          # 406 tests, ~8 min, no GPU needed (PHOTOINTEL_TEST_RESTIC=<restic.exe> adds the real-restic test)
python -m photointel add-root "D:/Photos"
python -m photointel index                     # resumable; only processes what changed
python -m photointel index --post-only --full-recluster   # after changing clustering settings
python -m photointel ocr [--all]               # read text in photos (post-index reads likely-text ones)
python -m photointel export-xmp <folder>       # XMP sidecars for other apps (refuses folders in a root)
python -m photointel import-gpx <files>        # GPS tracks, then --post-only --stages gpx,geocode,events,search-index
python -m photointel export <folder> --person A --person B   # copies only; --people-mode each|together|any
python -m photointel set-password              # required before `serve --host 0.0.0.0` (or accounts, or --insecure)
python -m photointel backup [<folder>]         # copy-only mirror of every root + the database to another drive
python -m photointel accounts list|reset-password <name>|reset-pin   # recovery at the computer
python -m photointel serve                     # http://127.0.0.1:8765 (+ https on 8443 once set up)
python -m photointel run                       # serve under the keeper: restarts it if it stops (what autostart runs)
python -m photointel autostart on|off|status   # start with the computer (--at-boot: Task Scheduler, admin)
python -m photointel health                    # drives, space, backups, phones, HTTPS, off-site
python -m photointel offsite setup|run|status|snapshots|check|restore   # encrypted off-site copy (restic)
python eval/inspect_library.py --data ./data   # sanity report for a real library
```

Rebuild the UI with `npm run build` in `web/`. Screenshot-review every screen with
`node web/shots.mjs <outdir> http://127.0.0.1:8765` (prefix `PW_CHANNEL=msedge` if Playwright's
Chromium is not installed) — it fails on console errors, failed API
requests, and a non-virtualising photo grid.

## Environment

Nothing is assumed to be on PATH. `PHOTOINTEL_DATA` picks the library; `PHOTOINTEL_MODELS` and
`PHOTOINTEL_GEO` let several libraries share one copy of the weights and place data;
`PHOTOINTEL_DEVICE` forces `cuda`/`cpu` for one run; `PHOTOINTEL_DEV=1` enables dev-server CORS.

`PI_DATASETS` points the `eval/` scripts at the COCO/LFW datasets (default `D:/pi_cache/datasets`).
Nothing under `photointel/` or `web/src/` hardcodes a path.

## Two habits this codebase demands

1. **When a metric looks bad, check the ground truth first.** Four times here a "bug" was a defect
   in the evaluation, not the system. Open a failing example before touching engine code.
2. **Verify a regression test fails against the old behaviour.** One written here passed both
   before and after the fix, which made it worthless.
