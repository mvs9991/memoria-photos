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
                          workers=args.workers, full_recluster=args.full_recluster)
    print(json.dumps(stats, indent=2, default=str))


def cmd_serve(ctx: AppContext, args) -> None:
    import uvicorn

    from .api.app import create_app

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
    from .vision.captioner import caption_photos

    conn = ctx.connect()
    try:
        print(json.dumps(caption_photos(ctx, conn, limit=args.limit, detailed=args.detailed,
                                        progress=lambda d, t: print(f"  {d}/{t}", flush=True)), indent=2))
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

    p = sub.add_parser("serve", help="run the web application")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser("geo-setup", help="download offline geocoding data")
    p.add_argument("--countries", help="ISO codes to enrich, e.g. IN,US")
    p.add_argument("--auto", action="store_true", help="enrich countries already present in the library")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("models", help="download or warm up models")
    p.add_argument("action", choices=["download", "warm"])

    p = sub.add_parser("caption", help="describe photos with a local vision model")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--detailed", action="store_true")

    sub.add_parser("status", help="library summary")

    args = parser.parse_args(argv)
    ctx = AppContext(args.data)
    setup_logging(ctx.paths, level=logging.DEBUG if args.verbose else logging.INFO)
    handlers = {"add-root": cmd_add_root, "index": cmd_index, "serve": cmd_serve, "status": cmd_status,
                "geo-setup": cmd_geo_setup, "models": cmd_models, "caption": cmd_caption}
    t0 = time.time()
    handlers[args.cmd](ctx, args)
    log.debug("%s finished in %.1fs", args.cmd, time.time() - t0)


if __name__ == "__main__":
    main()
