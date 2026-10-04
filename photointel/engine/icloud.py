"""iCloud Photos export awareness: favourites, hidden and deleted flags, dates, albums.

Apple's "Get a copy of your data" download for iCloud Photos is a set of folders
("iCloud Photos Part 1 of N"), each with the media files, a `Photo Details*.csv`
(columns include imgName, favorite, hidden, deleted, originalCreationDate) and an
`Albums/` folder holding one CSV per album that lists its image names.

The format is taken from Apple's published layout and exports people have described;
it has **not been checked against a real export here**, so parsing is deliberately
tolerant (any CSV with an `imgName` column; album CSVs are any single-column CSV under a
folder named Albums) and every decision is conservative, like the Takeout importer:

* **Dates: only as a last resort** — used only for a photo otherwise dated by file
  modification time (which, in an unzipped download, is the day it was extracted).
* **Flags are applied once**, on first import, so a later change here is never undone.
  Favourite → favourite; Hidden (Apple's private album) → the Locked folder; Recently
  Deleted → hidden (never deleted: that is the user's call, in the Trash).
* **Matching is by file name within the same export**; a name that matches more than one
  photo there is skipped, not guessed.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .. import db
from ..metadata import naive_to_ts
from . import albums as albums_mod

log = logging.getLogger(__name__)

PART = re.compile(r"^icloud photos( part \d+ of \d+)?$", re.I)
_DATE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)?", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}


def parse_date(text: str | None) -> float | None:
    """'Saturday June 26,2021 10:25 AM GMT' or ISO 8601 -> UTC epoch seconds."""
    if not text or not text.strip():
        return None
    t = text.strip()
    m = _DATE.search(t)
    if m:
        word = m.group(1).lower()
        mon = next((n for name, n in MONTHS.items() if name.startswith(word[:3])), 0)
        if not mon:
            return None
        hour = int(m.group(4))
        if m.group(7):                                  # 12-hour clock
            hour = hour % 12 + (12 if m.group(7).upper() == "PM" else 0)
        try:
            dt = datetime(int(m.group(3)), mon, int(m.group(2)), hour, int(m.group(5)), int(m.group(6) or 0),
                          tzinfo=timezone.utc)
        except ValueError:
            return None
        return dt.timestamp()
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def _yes(v: str | None) -> bool:
    return (v or "").strip().lower() in ("yes", "true", "1")


def _scope(folder: str) -> tuple[str, str]:
    """-> (export folder, part folder): the photos a CSV may refer to live under the export."""
    parts = PurePosixPath(folder).parts
    for i in range(len(parts) - 1, -1, -1):
        if PART.match(parts[i]):
            return "/".join(parts[:i]), "/".join(parts[:i + 1])
    parent = str(PurePosixPath(folder).parent)
    parent = "" if parent == "." else parent
    return parent, folder


def import_icloud(ctx, conn: sqlite3.Connection) -> dict:
    t0 = time.time()
    stats: Counter = Counter()
    folders = conn.execute("SELECT DISTINCT p.root_id, r.path AS root, p.folder FROM photos p JOIN roots r "
                           "ON r.id = p.root_id WHERE p.status NOT IN ('missing', 'deleted')").fetchall()
    # Every folder that holds media, plus its parents, may hold the CSVs.
    dirs: set[tuple[int, str, str]] = set()
    for f in folders:
        pp = PurePosixPath(f["folder"])
        for d in (pp, *pp.parents):
            s = "" if str(d) == "." else str(d)
            dirs.add((int(f["root_id"]), f["root"], s))
    # An export's Albums/ folder holds only CSVs, so no photo lives in it or under it: add it.
    for rid, root, folder in list(dirs):
        albums_dir = f"{folder}/Albums" if folder else "Albums"
        if os.path.isdir(os.path.join(root, albums_dir)):
            dirs.add((rid, root, albums_dir))
    details: list[tuple[int, str, str]] = []      # (root_id, folder, csv file)
    album_csvs: list[tuple[int, str, str]] = []
    for rid, root, folder in dirs:
        try:
            names = [e.name for e in os.scandir(os.path.join(root, folder)) if e.is_file() and e.name.lower().endswith(".csv")]
        except OSError:
            continue
        for n in names:
            if n.lower().startswith("photo details"):
                details.append((rid, folder, n))
            elif PurePosixPath(folder).name.lower() == "albums":
                album_csvs.append((rid, folder, n))
    if not details and not album_csvs:
        return {"csv": 0}

    index_cache: dict[tuple[int, str], dict[str, list[tuple[int, str]]]] = {}

    def names_in(rid: int, export: str) -> dict[str, list[tuple[int, str]]]:
        key = (rid, export)
        if key not in index_cache:
            like = db.like_prefix(export) if export else "%"
            idx: dict[str, list[tuple[int, str]]] = defaultdict(list)
            for r in conn.execute("SELECT id, filename, folder FROM photos WHERE root_id = ? AND "
                                  "(folder = ? OR folder LIKE ? ESCAPE '\\') AND status NOT IN ('missing', 'deleted')",
                                  (rid, export, like)):
                idx[r["filename"].lower()].append((int(r["id"]), r["folder"]))
            index_cache[key] = idx
        return index_cache[key]

    def match(rid: int, folder: str, name: str) -> int | None:
        export, part = _scope(folder)
        cands = names_in(rid, export).get((name or "").strip().lower(), [])
        if len(cands) > 1:
            same_part = [c for c in cands if c[1] == part or c[1].startswith(part + "/")]
            cands = same_part if len(same_part) == 1 else cands
        if len(cands) != 1:
            if cands:
                stats["ambiguous"] += 1
            return None
        return cands[0][0]

    done = {int(r[0]) for r in conn.execute("SELECT photo_id FROM icloud_items")}
    for rid, folder, fname in details:
        root = conn.execute("SELECT path FROM roots WHERE id = ?", (rid,)).fetchone()[0]
        for row in _read_csv(os.path.join(root, folder, fname)):
            name = row.get("imgname") or row.get("filename")
            if not name:
                continue
            pid = match(rid, folder, name)
            if pid is None:
                stats["unmatched"] += 1
                continue
            _apply(conn, pid, row, first_time=pid not in done, stats=stats)
            done.add(pid)
            stats["photos"] += 1
        conn.commit()

    for rid, folder, fname in album_csvs:
        root = conn.execute("SELECT path FROM roots WHERE id = ?", (rid,)).fetchone()[0]
        path = os.path.join(root, folder, fname)
        members = []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for i, line in enumerate(csv.reader(fh)):
                if not line or (i == 0 and line[0].strip().lower() in ("images", "imgname", "image name")):
                    continue
                pid = match(rid, folder, line[0])
                if pid is not None:
                    members.append(pid)
        if not members:
            continue
        key = f"icloud:{rid}:{folder}/{fname}"
        existing = conn.execute("SELECT id, hidden FROM albums WHERE source_key = ?", (key,)).fetchone()
        if existing is None:
            aid = albums_mod.create_album(conn, os.path.splitext(fname)[0], source="icloud", source_key=key)
            stats["albums"] += 1
        elif existing["hidden"]:
            continue                    # the user deleted it here; do not bring it back
        else:
            aid = int(existing["id"])
        albums_mod.add_photos(conn, aid, members, commit=False)
        conn.commit()
    out = dict(stats)
    out["csv"] = len(details) + len(album_csvs)
    out["seconds"] = round(time.time() - t0, 2)
    log.info("iCloud import: %s", out)
    return out


def _read_csv(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh, restkey="_extra")
            names = [(n or "").strip().lower() for n in reader.fieldnames or []]
            for raw in reader:
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()
                       if k != "_extra" and isinstance(v, str)}
                # A date like "June 26,2021" written without quotes splits into two columns:
                # join it back (the columns after it shift, but none of them is used).
                date = row.get("originalcreationdate", "")
                if raw.get("_extra") and date and not re.search(r"\d{4}", date) and "originalcreationdate" in names:
                    i = names.index("originalcreationdate")
                    if i + 1 < len(names):
                        row["originalcreationdate"] = f"{date},{row.get(names[i + 1], '')}"
                rows.append(row)
        return rows
    except (OSError, UnicodeDecodeError, csv.Error):
        log.warning("Unreadable iCloud CSV %s", path)
        return []


def _apply(conn: sqlite3.Connection, pid: int, row: dict, first_time: bool, stats: Counter) -> None:
    fav, hidden, deleted = _yes(row.get("favorite")), _yes(row.get("hidden")), _yes(row.get("deleted"))
    created = parse_date(row.get("originalcreationdate"))
    if first_time:
        if fav:
            from .favorites import imported
            imported(conn, pid)
            stats["favorites"] += 1
        if hidden:
            conn.execute("UPDATE photos SET locked = 1 WHERE id = ?", (pid,))
            stats["locked"] += 1
        if deleted:
            conn.execute("UPDATE photos SET hidden = 1 WHERE id = ?", (pid,))
            stats["recently_deleted_hidden"] += 1
        if fav or hidden or deleted:
            db.audit(conn, "icloud_flags", "photo", pid, {"favorite": fav, "hidden": hidden, "deleted": deleted},
                     actor="import")
        conn.execute("INSERT OR IGNORE INTO icloud_items(photo_id, created_ts, favorite, hidden, deleted, imported_at) "
                     "VALUES (?,?,?,?,?,?)", (pid, created, int(fav), int(hidden), int(deleted), time.time()))
    if created:
        p = conn.execute("SELECT date_source FROM photos WHERE id = ?", (pid,)).fetchone()
        if p and p["date_source"] == "mtime":
            local = datetime.fromtimestamp(created)
            conn.execute("UPDATE photos SET taken_ts = ?, taken_local = ?, date_source = 'icloud', "
                         "date_confidence = 'medium' WHERE id = ?",
                         (naive_to_ts(local), local.strftime("%Y-%m-%d %H:%M:%S"), pid))
            stats["dates_from_csv"] += 1
