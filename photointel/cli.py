"""Command-line interface: `python -m photointel <command>`."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .context import AppContext, setup_logging

log = logging.getLogger("photointel.cli")


def cmd_add_root(ctx: AppContext, args) -> None:
    from pathlib import Path

    from .pipeline.scanner import ensure_root

    conn = ctx.connect()
    for r in args.paths:
        p = Path(r)
        if not p.is_dir():
            print(f"Not a directory: {r}", file=sys.stderr)
            continue
        rid = ensure_root(conn, p)
        print(f"Root #{rid}: {p.resolve()}")
    conn.close()


def cmd_index(ctx: AppContext, args) -> None:
    from .pipeline.jobs import run_index_job

    stats = run_index_job(ctx, job_id=args.job_id, roots=args.roots or None, retry_errors=args.retry_errors,
                          skip_faces=args.no_faces, skip_semantic=args.no_semantic, post_only=args.post_only,
                          workers=args.workers, full_recluster=args.full_recluster,
                          post_stages=[s.strip() for s in args.stages.split(",")] if args.stages else None)
    print(json.dumps(stats, indent=2, default=str))


def cmd_serve(ctx: AppContext, args) -> None:
    import uvicorn

    from .api.app import create_app
    from .auth import is_loopback

    if not is_loopback(args.host) and not ctx.settings.access_password_hash and not args.insecure:
        print("Refusing to serve beyond this machine without a password: anyone on the network could "
              "browse every photo.\nSet one first:  python -m photointel set-password\n"
              "(or pass --insecure if you really mean it)", file=sys.stderr)
        sys.exit(2)

    app = create_app(ctx)
    print(f"Memoria running at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_geo_setup(ctx: AppContext, args) -> None:
    from .geodata import countries_in_library, fetch_base, fetch_countries

    print(json.dumps(fetch_base(ctx.paths.geo, force=args.force), indent=2))
    countries = [c.strip().upper() for c in (args.countries or "").split(",") if c.strip()]
    if args.auto:
        conn = ctx.connect()
        countries += countries_in_library(conn)
        conn.close()
    if countries:
        print(json.dumps(fetch_countries(ctx.paths.geo, sorted(set(countries)), force=args.force), indent=2))


def cmd_models(ctx: AppContext, args) -> None:
    from .models import download_face_models, warm_models

    if args.action == "download":
        print(json.dumps(download_face_models(ctx.paths.models), indent=2))
    else:
        print(json.dumps(warm_models(ctx), indent=2, default=str))


def cmd_caption(ctx: AppContext, args) -> None:
    from .pipeline.jobs import run_caption_job

    print(json.dumps(run_caption_job(ctx, job_id=args.job_id, limit=args.limit, detailed=args.detailed),
                     indent=2))


def cmd_ocr(ctx: AppContext, args) -> None:
    from .pipeline.jobs import run_ocr_job

    print(json.dumps(run_ocr_job(ctx, job_id=args.job_id, everything=args.all, limit=args.limit), indent=2))


def cmd_export_xmp(ctx: AppContext, args) -> None:
    from .engine.xmp import export_xmp

    conn = ctx.connect()
    try:
        print(json.dumps(export_xmp(conn, args.out, include_auto_tags=args.auto_tags, everything=args.all), indent=2))
    finally:
        conn.close()


def cmd_set_password(ctx: AppContext, args) -> None:
    import getpass

    from .auth import hash_password, mark_password_changed

    pw = getpass.getpass("New password (empty removes it): ")
    if pw and pw != getpass.getpass("Again: "):
        print("Passwords differ; nothing changed.", file=sys.stderr)
        sys.exit(1)
    ctx.settings.access_password_hash = hash_password(pw) if pw else ""
    ctx.settings.save(ctx.paths.data)
    mark_password_changed(ctx.paths.data)
    print("Password set." if pw else "Password removed; the app is open to anyone who can reach it.")


def cmd_import_gpx(ctx: AppContext, args) -> None:
    from pathlib import Path

    from .engine.gpx import GpxError, import_file

    conn = ctx.connect()
    for f in args.files:
        try:
            print(f"Track #{import_file(ctx, conn, Path(f))}: {f}")
        except (GpxError, OSError) as exc:
            print(f"Skipped {f}: {exc}", file=sys.stderr)
    conn.close()
    print("Run `index --post-only --stages gpx,geocode,events,search-index` to place photos by these tracks.")


def cmd_export(ctx: AppContext, args) -> None:
    from .engine.export import ExportSpec, export_to_folder
    from .engine.xmp import ExportError

    conn = ctx.connect()
    try:
        people = []
        for p in args.person or []:
            row = conn.execute("SELECT id FROM persons WHERE merged_into IS NULL AND (CAST(id AS TEXT) = ? "
                               "OR lower(name) = lower(?))", (p, p)).fetchone()
            if row is None:
                print(f"No person called {p!r} (use a name or an id from the People page)", file=sys.stderr)
                sys.exit(1)
            people.append(int(row[0]))
        album = None
        if args.album:
            row = conn.execute("SELECT id FROM albums WHERE hidden = 0 AND kind = 'manual' AND "
                               "(CAST(id AS TEXT) = ? OR lower(name) = lower(?))", (args.album, args.album)).fetchone()
            if row is None:
                print(f"No album {args.album!r}", file=sys.stderr)
                sys.exit(1)
            album = int(row[0])
        spec = ExportSpec.from_dict({
            "person_ids": people or None, "person_mode": args.people_mode, "album_id": album, "event_id": args.event,
            "year": args.year, "month": args.month, "layout": args.layout, "xmp": args.xmp,
            "include_stack_frames": args.all_frames, "folder": args.out})
        print(json.dumps(export_to_folder(conn, spec, progress=lambda d, t: print(f"copied {d:,}/{t:,}", end=chr(13))),
                         indent=2, default=str))
    except ExportError as exc:
        print(f"Export refused: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def cmd_trash(ctx: AppContext, args) -> None:
    """List or restore the trash, or erase what has expired. Deleting itself is only done in the app."""
    from .engine import trash

    conn = ctx.connect()
    try:
        if args.action == "list":
            for t in trash.list_trash(conn):
                days = max(0, int((t["expires_at"] - time.time()) // 86400))
                print(f"#{t['photo_id']:>7}  {days:>3} days left  {t['original_path']}")
        elif args.action == "restore":
            print(json.dumps(trash.restore(ctx, conn, [int(x) for x in args.ids]), indent=2))
        else:
            print(json.dumps(trash.purge_expired(ctx, conn), indent=2))
    finally:
        conn.close()


def cmd_backup(ctx: AppContext, args) -> None:
    from .engine.backup import run_backup
    from .engine.xmp import ExportError

    target = args.folder or ctx.settings.backup_folder
    if not target:
        print("Give a folder, or set one in Settings → Backup", file=sys.stderr)
        sys.exit(1)
    conn = ctx.connect()
    try:
        print(json.dumps(run_backup(ctx, conn, target, progress=lambda d, t: print(f"checked {d:,}/{t:,}", end=chr(13))),
                         indent=2, default=str))
    except ExportError as exc:
        print(f"Backup refused: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def cmd_status(ctx: AppContext, args) -> None:
    conn = ctx.connect()
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    out = {
        "roots": [dict(r) for r in conn.execute("SELECT id, path, last_scan_at FROM roots")],
        "photos": q("SELECT COUNT(*) FROM photos"),
        "by_status": {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM photos GROUP BY status")},
        "faces": q("SELECT COUNT(*) FROM faces"),
        "persons": q("SELECT COUNT(*) FROM persons WHERE merged_into IS NULL"),
        "events": q("SELECT COUNT(*) FROM events"),
        "dup_groups": q("SELECT COUNT(*) FROM dup_groups"),
        "errors": q("SELECT COUNT(*) FROM processing_errors"),
    }
    print(json.dumps(out, indent=2, default=str))
    conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="photointel", description="Memoria — local photo intelligence")
    parser.add_argument("--data", help="data directory (default: ./data or $PHOTOINTEL_DATA)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-root", help="register a photo library root folder")
    p.add_argument("paths", nargs="+")

    p = sub.add_parser("index", help="scan roots and analyse new/changed photos")
    p.add_argument("roots", nargs="*", help="roots to scan (default: all registered roots)")
    p.add_argument("--job-id", type=int)
    p.add_argument("--retry-errors", action="store_true")
    p.add_argument("--no-faces", action="store_true")
    p.add_argument("--no-semantic", action="store_true")
    p.add_argument("--post-only", action="store_true", help="only run clustering/events/duplicates")
    p.add_argument("--full-recluster", action="store_true",
                   help="rebuild every auto-discovered person from scratch (your named people and "
                        "corrections are kept); needed after changing clustering settings")
    p.add_argument("--workers", type=int)
    p.add_argument("--stages", help="with --post-only: comma-separated post stages to run (default: all)")

    p = sub.add_parser("serve", help="run the web application")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--insecure", action="store_true", help="allow a non-local host without a password")

    sub.add_parser("set-password", help="require a password for the web app (empty input removes it)")

    p = sub.add_parser("import-gpx", help="add GPS tracks, used to place photos that have no GPS")
    p.add_argument("files", nargs="+")

    p = sub.add_parser("geo-setup", help="download offline geocoding data")
    p.add_argument("--countries", help="ISO codes to enrich, e.g. IN,US")
    p.add_argument("--auto", action="store_true", help="enrich countries already present in the library")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("models", help="download or warm up models")
    p.add_argument("action", choices=["download", "warm"])

    p = sub.add_parser("caption", help="describe photos with a local vision model")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--detailed", action="store_true")
    p.add_argument("--job-id", type=int)

    p = sub.add_parser("ocr", help="read text in photos (by default only ones likely to contain text)")
    p.add_argument("--all", action="store_true", help="read every photo, not just likely-text ones")
    p.add_argument("--limit", type=int)
    p.add_argument("--job-id", type=int)

    p = sub.add_parser("export-xmp", help="write XMP sidecars (people, tags, ratings) to a separate folder")
    p.add_argument("out", help="export folder (must be outside your photo folders)")
    p.add_argument("--auto-tags", action="store_true", help="also export high-confidence automatic tags")
    p.add_argument("--all", action="store_true", help="a sidecar for every photo, not only annotated ones")

    p = sub.add_parser("export", help="copy originals (a person, people, album, event or year) to a folder")
    p.add_argument("out", help="export folder (must be outside your photo folders)")
    p.add_argument("--person", action="append", help="a person's name or id; repeat for several")
    p.add_argument("--people-mode", choices=["each", "together", "any"], default="each",
                   help="several people: a folder each (default), only photos with all of them, or any of them")
    p.add_argument("--album", help="album name or id")
    p.add_argument("--event", type=int, help="event or trip id")
    p.add_argument("--year", type=int)
    p.add_argument("--month", type=int)
    p.add_argument("--layout", choices=["date", "flat", "original"], default="date",
                   help="YYYY/MM folders (default), one folder, or the original folder structure")
    p.add_argument("--xmp", action="store_true", help="write a .xmp beside each copy (people, tags, stars)")
    p.add_argument("--all-frames", action="store_true", help="every file of RAW+JPEG pairs and bursts")

    p = sub.add_parser("trash", help="list or restore deleted files, or erase the expired ones")
    p.add_argument("action", choices=["list", "restore", "purge-expired"])
    p.add_argument("ids", nargs="*", help="photo ids to restore")

    p = sub.add_parser("backup", help="copy every photo folder and Memoria's data to another drive (copy-only)")
    p.add_argument("folder", nargs="?", help="backup folder (default: the one set in Settings)")

    sub.add_parser("status", help="library summary")

    args = parser.parse_args(argv)
    ctx = AppContext(args.data)
    setup_logging(ctx.paths, level=logging.DEBUG if args.verbose else logging.INFO)
    handlers = {"add-root": cmd_add_root, "index": cmd_index, "serve": cmd_serve, "status": cmd_status,
                "geo-setup": cmd_geo_setup, "models": cmd_models, "caption": cmd_caption, "ocr": cmd_ocr,
                "export-xmp": cmd_export_xmp,
                "set-password": cmd_set_password, "import-gpx": cmd_import_gpx, "export": cmd_export,
                "trash": cmd_trash, "backup": cmd_backup}
    t0 = time.time()
    handlers[args.cmd](ctx, args)
    log.debug("%s finished in %.1fs", args.cmd, time.time() - t0)


if __name__ == "__main__":
    main()
