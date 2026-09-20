"""Sanity report for a real library, where there is no ground truth.

The other eval scripts grade against datasets with known answers. Your own photos
have no answer key, so this asks a different question: does the result look
*plausible*, and is anything obviously wrong? It prints distributions and raises
warnings for the failure modes that matter — a cluster that swallowed half the
library, dates in the future, places on the wrong continent, everything landing
in one giant event.

    python eval/inspect_library.py --data D:/pi_cache/realdata

Read-only. It never modifies the library.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def head(title: str) -> None:
    print(f"\n=== {title} ===")


def row(label: str, value) -> None:
    print(f"  {label:<34} {value}")


def pct(n: int, total: int) -> str:
    return f"{n} ({n / total * 100:.1f}%)" if total else str(n)


def inspect_files(conn) -> int:
    head("FILES")
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    if not total:
        print("  empty library")
        return 0
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM photos GROUP BY status"))
    row("photos", total)
    for st, n in sorted(by_status.items()):
        row(f"  status {st}", pct(n, total))
    errs = by_status.get("error", 0)
    if errs / total > 0.01:
        warn(f"{errs / total:.1%} of files failed to process — check processing_errors")
    row("formats", ", ".join(f"{e}:{n}" for e, n in
                             conn.execute("SELECT ext, COUNT(*) FROM photos GROUP BY ext "
                                          "ORDER BY 2 DESC LIMIT 8")))
    row("provenance", ", ".join(f"{k or '?'}:{n}" for k, n in
                                conn.execute("SELECT source_kind, COUNT(*) FROM photos "
                                             "GROUP BY source_kind ORDER BY 2 DESC LIMIT 8")))
    for stage, n, msg in conn.execute(
            "SELECT stage, COUNT(*), MIN(error) FROM processing_errors GROUP BY stage ORDER BY 2 DESC LIMIT 5"):
        row(f"  error/{stage}", f"{n}  e.g. {str(msg)[:70]}")
    return total


def inspect_dates(conn, total: int) -> None:
    head("DATES")
    dated = conn.execute("SELECT COUNT(*) FROM photos WHERE taken_ts IS NOT NULL").fetchone()[0]
    row("with a capture date", pct(dated, total))
    row("date source", ", ".join(f"{s or '?'}:{n}" for s, n in
                                 conn.execute("SELECT date_source, COUNT(*) FROM photos "
                                              "GROUP BY date_source ORDER BY 2 DESC")))
    lo, hi = conn.execute("SELECT MIN(taken_ts), MAX(taken_ts) FROM photos "
                          "WHERE taken_ts IS NOT NULL").fetchone()
    if lo:
        fmt = "%Y-%m-%d"
        row("range", f"{datetime.utcfromtimestamp(lo):{fmt}} .. {datetime.utcfromtimestamp(hi):{fmt}}")
        now = datetime.now(timezone.utc).timestamp()
        future = conn.execute("SELECT COUNT(*) FROM photos WHERE taken_ts > ?", (now + 86400,)).fetchone()[0]
        ancient = conn.execute("SELECT COUNT(*) FROM photos WHERE taken_ts < ?",
                               (datetime(1990, 1, 1).timestamp(),)).fetchone()[0]
        if future:
            warn(f"{future} photos are dated in the future")
        if ancient:
            warn(f"{ancient} photos are dated before 1990 (likely a bad EXIF or epoch fallback)")
    # A year holding almost everything usually means mtime stood in for a real date.
    years = Counter(datetime.utcfromtimestamp(t).year for (t,) in
                    conn.execute("SELECT taken_ts FROM photos WHERE taken_ts IS NOT NULL"))
    if years:
        top_year, top_n = years.most_common(1)[0]
        row("busiest year", f"{top_year}: {pct(top_n, dated)}")
        if dated and top_n / dated > 0.6:
            warn(f"{top_n / dated:.0%} of photos fall in {top_year} — dates may be falling back to file mtime")


def inspect_places(conn, total: int) -> None:
    head("PLACES")
    gps = conn.execute("SELECT COUNT(*) FROM photos WHERE gps_lat IS NOT NULL").fetchone()[0]
    placed = conn.execute("SELECT COUNT(*) FROM photos WHERE place_id IS NOT NULL").fetchone()[0]
    row("with GPS", pct(gps, total))
    row("resolved to a place", pct(placed, total))
    if gps and not placed:
        warn("photos have GPS but none resolved to a place — is the geo data installed?")
    row("confidence", ", ".join(f"{c or '?'}:{n}" for c, n in
                                conn.execute("SELECT location_confidence, COUNT(*) FROM photos "
                                             "WHERE place_id IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")))
    for name, country, n in conn.execute(
            """SELECT pl.name, pl.country, COUNT(*) FROM photos p JOIN places pl ON pl.id = p.place_id
               GROUP BY pl.id ORDER BY 3 DESC LIMIT 8"""):
        row(f"  {name}, {country}", n)
    countries = conn.execute(
        """SELECT COUNT(DISTINCT pl.country) FROM photos p JOIN places pl ON pl.id = p.place_id""").fetchone()[0]
    row("distinct countries", countries)


def inspect_people(conn) -> None:
    head("PEOPLE")
    faces = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    if not faces:
        print("  no faces detected")
        return
    assigned = conn.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL").fetchone()[0]
    people = conn.execute("SELECT COUNT(*) FROM persons WHERE merged_into IS NULL").fetchone()[0]
    row("faces", faces)
    row("assigned to a person", pct(assigned, faces))
    row("people discovered", people)
    if faces and assigned / faces < 0.25:
        warn(f"only {assigned / faces:.0%} of faces were grouped — clustering may be too strict")

    sizes = [n for (n,) in conn.execute(
        "SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL GROUP BY person_id ORDER BY 1 DESC")]
    if sizes:
        row("largest person (faces)", sizes[0])
        row("median person (faces)", sizes[len(sizes) // 2])
        row("people with >= 10 faces", sum(1 for s in sizes if s >= 10))
        # One identity absorbing a large share of all faces is the classic over-merge.
        if sizes[0] / faces > 0.30:
            warn(f"one person holds {sizes[0] / faces:.0%} of all faces — likely an over-merged cluster")
    q = conn.execute("SELECT AVG(quality), AVG(det_score), AVG(size_px) FROM faces").fetchone()
    row("avg quality / det / size_px", f"{q[0]:.2f} / {q[1]:.2f} / {q[2]:.0f}px")
    for pid, name, n in conn.execute(
            """SELECT id, COALESCE(name, 'Person ' || COALESCE(display_no, id)), face_count
               FROM persons WHERE merged_into IS NULL ORDER BY face_count DESC LIMIT 8"""):
        row(f"  #{pid} {name}", f"{n} faces")


def inspect_events(conn, total: int) -> None:
    head("EVENTS")
    ev = conn.execute("SELECT COUNT(*) FROM events WHERE kind='event'").fetchone()[0]
    trips = conn.execute("SELECT COUNT(*) FROM events WHERE kind='trip'").fetchone()[0]
    grouped = conn.execute("SELECT COUNT(*) FROM photos WHERE event_id IS NOT NULL").fetchone()[0]
    row("events / trips", f"{ev} / {trips}")
    row("photos in an event", pct(grouped, total))
    sizes = [n for (n,) in conn.execute(
        "SELECT COUNT(*) FROM photos WHERE event_id IS NOT NULL GROUP BY event_id ORDER BY 1 DESC")]
    if sizes:
        row("largest / median event", f"{sizes[0]} / {sizes[len(sizes) // 2]} photos")
        if grouped and sizes[0] / grouped > 0.5:
            warn(f"one event holds {sizes[0] / grouped:.0%} of grouped photos — segmentation may have failed")
    # A trip's photos hang off its child events, not off the trip row itself.
    for title, n in conn.execute(
            """SELECT COALESCE(user_title, auto_title),
                      (SELECT COUNT(*) FROM trip_photos tp WHERE tp.trip_id = e.id) n
               FROM events e WHERE kind='trip' ORDER BY n DESC LIMIT 6"""):
        row(f"  trip: {title}", f"{n} photos")


def inspect_duplicates(conn, total: int) -> None:
    head("DUPLICATES")
    groups = conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0]
    row("groups", groups)
    row("by kind", ", ".join(f"{k}:{n}" for k, n in
                             conn.execute("SELECT kind, COUNT(*) FROM dup_groups GROUP BY 1 ORDER BY 2 DESC")))
    # A photo can belong to one group per kind, so counting membership rows
    # double-counts and can exceed 100%.
    members = conn.execute("SELECT COUNT(DISTINCT photo_id) FROM dup_members").fetchone()[0]
    row("photos in a group", pct(members, total))
    for kind in ("exact", "near", "likely", "similar"):
        n = conn.execute("SELECT COUNT(DISTINCT m.photo_id) FROM dup_members m "
                         "JOIN dup_groups g ON g.id = m.group_id WHERE g.kind = ?", (kind,)).fetchone()[0]
        if n:
            row(f"  {kind}", pct(n, total))
    # sha256 is ground truth, not a threshold: this number needs no interpretation.
    distinct = conn.execute("SELECT COUNT(DISTINCT sha256) FROM photos WHERE sha256 IS NOT NULL").fetchone()[0]
    hashed = conn.execute("SELECT COUNT(*) FROM photos WHERE sha256 IS NOT NULL").fetchone()[0]
    row("byte-identical redundancy", pct(hashed - distinct, hashed))
    redundant = conn.execute(
        """SELECT COALESCE(SUM(p.size), 0) FROM dup_members m JOIN photos p ON p.id = m.photo_id
           JOIN dup_groups g ON g.id = m.group_id
           WHERE g.kind != 'similar' AND p.id != g.keep_photo_id""").fetchone()[0]
    row("redundant bytes (exact/near/likely)", f"{redundant / 1e9:.2f} GB")
    # Exact matches are proven by hash, so a high share of them is a fact about the
    # library (overlapping exports, copied folders), not a sign of loose thresholds.
    # Only the fuzzy kinds are threshold-dependent and worth flagging.
    fuzzy = conn.execute(
        "SELECT COUNT(DISTINCT m.photo_id) FROM dup_members m JOIN dup_groups g ON g.id = m.group_id "
        "WHERE g.kind IN ('likely','similar')").fetchone()[0]
    if total and fuzzy / total > 0.75:
        warn(f"{fuzzy / total:.0%} of photos are in a likely/similar group — those thresholds may be too loose")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="library data directory")
    args = ap.parse_args()
    db_path = Path(args.data) / "library.db"
    if not db_path.exists():
        print(f"no library at {db_path}")
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print(f"Library: {db_path}")
    total = inspect_files(conn)
    if total:
        inspect_dates(conn, total)
        inspect_places(conn, total)
        inspect_people(conn)
        inspect_events(conn, total)
        inspect_duplicates(conn, total)

    head("WARNINGS")
    if WARNINGS:
        for w in WARNINGS:
            print(f"  ! {w}")
    else:
        print("  none — nothing obviously wrong")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
