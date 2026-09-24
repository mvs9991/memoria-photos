"""Google Takeout awareness: albums, descriptions, locations, flags and people names.

What a sidecar is trusted for, and why (see HANDOFF.md §4):

* **Dates: only as a last resort.** Measured on 6,415 real sidecars, `photoTakenTime`
  was the *worse* answer whenever it disagreed with EXIF/filename (it often records the
  upload). It is used only for a photo that would otherwise be dated by file
  modification time — which in an unzipped Takeout is the day it was extracted.
* **Location:** only for photos with no GPS of their own, at medium confidence.
* **Description:** text a person typed in Google Photos; searchable.
* **Favourite / trash:** applied once, on first import, so a later change made here
  is never overwritten by re-reading the sidecar.
* **People names are never applied.** A sidecar says who is in a photo, not which face
  is whom. They are only offered as *suggestions* for a person Memoria already found,
  with the evidence shown, for the user to accept.
* **Albums:** Takeout writes each album as a folder of byte-identical copies. They
  become albums (membership by content, so hiding the duplicate copies is safe).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import PurePosixPath

from .. import db
from ..metadata import naive_to_ts
from . import albums as albums_mod

log = logging.getLogger(__name__)

YEAR_FOLDER = re.compile(r"^Photos from (18|19|20)\d{2}$")
SYSTEM_FOLDERS = {"trash", "bin", "archive", "failed videos"}
NOT_SIDECARS = {"metadata.json", "print-subscriptions.json", "shared_album_comments.json",
                "user-generated-memory-titles.json"}
SUPPLEMENTAL = ".supplemental-metadata"
# Google's "edited" suffixes (the list GooglePhotosTakeoutHelper collected, per locale).
EDIT_SUFFIXES = ("-edited", "-effects", "-smile", "-mix", "-edytowane", "-bearbeitet", "-bewerkt",
                 "-編集済み", "-modificato", "-modifié", "-ha editado", "-editat")
MIN_TRUNCATED = 30          # a truncated sidecar name still keeps at least this much

_NUM_AT_END = re.compile(r"^(.*?)(\(\d+\))?$")
_MEDIA_NAME = re.compile(r"^(.*?)(\(\d+\))?(\.[^.]+)$")


# ---------------------------------------------------------------------------- matching

class _FolderSidecars:
    """The .json sidecars of one folder, parsed for name matching."""

    def __init__(self, names: list[str]):
        self.entries: list[tuple[str, str | None, str]] = []   # (core lower, "(n)" or None, file name)
        for n in names:
            if n.lower() in NOT_SIDECARS or not n.lower().endswith(".json"):
                continue
            m = _NUM_AT_END.match(n[:-5])
            self.entries.append((m.group(1).lower(), m.group(2), n))

    def match(self, media_name: str) -> str | None:
        """File name of the sidecar describing `media_name`, or None."""
        m = _MEDIA_NAME.match(media_name)
        if not m:
            return None
        stem, num, ext = m.group(1), m.group(2), m.group(3)
        for base_stem in _stem_variants(stem):
            hit = self._match_one(f"{base_stem}{ext}".lower(), base_stem.lower(), num)
            if hit:
                return hit
        return None

    def _match_one(self, name: str, stem: str, num: str | None) -> str | None:
        full = name + SUPPLEMENTAL
        best, best_rank = None, 99
        for core, jnum, fname in self.entries:
            if jnum != num:
                continue
            if core == name:                       # IMG.jpg.json
                rank = 0
            elif core == full:                     # IMG.jpg.supplemental-metadata.json
                rank = 1
            elif full.startswith(core) and (len(core) > len(name) or len(core) >= MIN_TRUNCATED):
                rank = 2                           # truncated to fit 51 characters
            elif core == stem:                     # uploaded without an extension
                rank = 3
            else:
                continue
            if rank < best_rank or (rank == best_rank and best is not None and len(fname) > len(best)):
                best, best_rank = fname, rank
        return best


def _stem_variants(stem: str) -> list[str]:
    out = [stem]
    low = stem.lower()
    for suf in EDIT_SUFFIXES:
        if low.endswith(suf):
            out.append(stem[: -len(suf)])      # IMG-edited.jpg shares IMG.jpg's sidecar
            break
    return out


def parse_sidecar(data: dict) -> dict:
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    taken = num((data.get("photoTakenTime") or {}).get("timestamp"))
    geo = data.get("geoData") or {}
    lat, lon = num(geo.get("latitude")), num(geo.get("longitude"))
    if lat is None or lon is None or (abs(lat) < 1e-6 and abs(lon) < 1e-6) \
            or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    people = [str(p.get("name")).strip() for p in data.get("people") or []
              if isinstance(p, dict) and str(p.get("name") or "").strip()]
    return {
        "taken_ts": taken if taken and taken > 0 else None,
        "lat": lat, "lon": lon,
        "description": (data.get("description") or "").strip() or None,
        "people": sorted(set(people)),
        "favorited": bool(data.get("favorited")),
        "archived": bool(data.get("archived")),
        "trashed": bool(data.get("trashed")),
    }


# ---------------------------------------------------------------------------- import

def import_takeout(ctx, conn: sqlite3.Connection) -> dict:
    t0 = time.time()
    stats = Counter()
    folders = conn.execute(
        "SELECT DISTINCT p.root_id, r.path AS root, p.folder FROM photos p JOIN roots r ON r.id = p.root_id "
        "WHERE p.status != 'missing'").fetchall()
    by_root: dict[int, set[str]] = defaultdict(set)
    for f in folders:
        by_root[f["root_id"]].add(f["folder"])
    existing = {int(r[0]) for r in conn.execute("SELECT photo_id FROM takeout_sidecars")}
    now = time.time()

    for f in folders:
        abs_dir = os.path.join(f["root"], f["folder"])
        try:
            names = [e.name for e in os.scandir(abs_dir) if e.is_file() and e.name.lower().endswith(".json")]
        except OSError:
            continue
        if not names:
            continue
        sidecars = _FolderSidecars(names)
        photos = conn.execute(
            "SELECT id, filename, gps_lat, date_source, description FROM photos "
            "WHERE root_id = ? AND folder = ? AND status != 'missing'", (f["root_id"], f["folder"])).fetchall()
        for p in photos:
            jname = sidecars.match(p["filename"])
            if not jname:
                continue
            try:
                with open(os.path.join(abs_dir, jname), encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError, UnicodeDecodeError):
                stats["unreadable"] += 1
                continue
            if not isinstance(raw, dict) or ("photoTakenTime" not in raw and "title" not in raw):
                continue    # some other JSON that happens to sit next to the photo
            sc = parse_sidecar(raw)
            _apply(conn, int(p["id"]), p, sc, first_time=int(p["id"]) not in existing, stats=stats)
            conn.execute(
                """INSERT INTO takeout_sidecars(photo_id, json_path, taken_ts, lat, lon, description, people,
                       favorited, archived, trashed, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(photo_id) DO UPDATE SET json_path=excluded.json_path, taken_ts=excluded.taken_ts,
                       lat=excluded.lat, lon=excluded.lon, description=excluded.description, people=excluded.people,
                       favorited=excluded.favorited, archived=excluded.archived, trashed=excluded.trashed""",
                (int(p["id"]), os.path.join(f["folder"], jname), sc["taken_ts"], sc["lat"], sc["lon"],
                 sc["description"], json.dumps(sc["people"]), int(sc["favorited"]), int(sc["archived"]),
                 int(sc["trashed"]), now))
            stats["sidecars"] += 1
        conn.commit()

    stats["albums"] = _import_albums(conn, folders, by_root)
    conn.commit()
    if stats["sidecars"]:
        db.bump_generation(conn, "people")   # descriptions/locations feed search vocabulary
    out = dict(stats)
    out["seconds"] = round(time.time() - t0, 2)
    log.info("Takeout import: %s", out)
    return out


def _apply(conn, photo_id: int, p, sc: dict, first_time: bool, stats: Counter) -> None:
    if first_time:
        if sc["favorited"]:
            conn.execute("UPDATE photos SET favorite = 1 WHERE id = ?", (photo_id,))
            stats["favorites"] += 1
        if sc["trashed"]:
            conn.execute("UPDATE photos SET hidden = 1 WHERE id = ?", (photo_id,))
            stats["trashed_hidden"] += 1
        if sc["favorited"] or sc["trashed"]:
            db.audit(conn, "takeout_flags", "photo", photo_id,
                     {"favorite": sc["favorited"], "hidden": sc["trashed"]}, actor="import")
    if sc["description"] and not p["description"]:
        conn.execute("UPDATE photos SET description = ? WHERE id = ?", (sc["description"], photo_id))
        stats["descriptions"] += 1
    if sc["lat"] is not None and p["gps_lat"] is None:
        conn.execute("UPDATE photos SET gps_lat = ?, gps_lon = ?, place_id = NULL, landmark_id = NULL, "
                     "location_source = 'takeout', location_confidence = 'medium' WHERE id = ?",
                     (sc["lat"], sc["lon"], photo_id))
        stats["locations"] += 1
    if sc["taken_ts"] and p["date_source"] == "mtime":
        # photoTakenTime is UTC; shown in this machine's zone, like the file browser would.
        local = datetime.fromtimestamp(sc["taken_ts"])
        conn.execute("UPDATE photos SET taken_ts = ?, taken_local = ?, date_source = 'takeout', "
                     "date_confidence = 'low' WHERE id = ?",
                     (naive_to_ts(local), local.strftime("%Y-%m-%d %H:%M:%S"), photo_id))
        stats["dates_from_sidecar"] += 1


def _import_albums(conn, folders, by_root: dict[int, set[str]]) -> int:
    n = 0
    for f in folders:
        folder = f["folder"]
        if not folder:
            continue
        pp = PurePosixPath(folder)
        name = pp.name
        if YEAR_FOLDER.match(name) or name.lower() in SYSTEM_FOLDERS:
            continue
        meta_title, meta_desc = _album_metadata(os.path.join(f["root"], folder))
        siblings = {PurePosixPath(x).name for x in by_root[f["root_id"]]
                    if str(PurePosixPath(x).parent) == str(pp.parent)}
        if meta_title is None and not any(YEAR_FOLDER.match(s) for s in siblings):
            continue    # an ordinary folder, not a Takeout album
        key = f"takeout:{f['root_id']}:{folder}"
        row = conn.execute("SELECT id, hidden FROM albums WHERE source_key = ?", (key,)).fetchone()
        if row is None:
            aid = albums_mod.create_album(conn, meta_title or name, description=meta_desc,
                                          source="takeout", source_key=key)
            n += 1
        elif row["hidden"]:
            continue    # the user deleted it; do not bring it back
        else:
            aid = int(row["id"])
        ids = [int(r[0]) for r in conn.execute(
            "SELECT id FROM photos WHERE root_id = ? AND folder = ? AND status != 'missing'",
            (f["root_id"], folder))]
        albums_mod.add_photos(conn, aid, ids, commit=False)
    return n


def _album_metadata(abs_dir: str) -> tuple[str | None, str | None]:
    path = os.path.join(abs_dir, "metadata.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    title = (data.get("title") or "").strip() if isinstance(data, dict) else ""
    desc = (data.get("description") or "").strip() if isinstance(data, dict) else ""
    return title or None, desc or None


# ---------------------------------------------------------------------------- name suggestions

def name_suggestions(conn: sqlite3.Connection, min_photos: int = 3, min_share: float = 0.6,
                     min_coverage: float = 0.5) -> list[dict]:
    """Suggest a Google Photos name for people Memoria found but nobody has named.

    For person P and name N (counted per distinct photo content, since album copies
    repeat the same sidecar):
      share    = P's photos labelled N / P's photos that have any sidecar people
      coverage = P's photos labelled N / all photos labelled N
    Both must be high: a name on most of P's photos but on far more photos without P is
    somebody else who is often photographed with P. Each name and each person is used
    once, strongest evidence first. Nothing is applied — the user accepts or dismisses.
    """
    rows = conn.execute(
        """SELECT COALESCE(p.sha256, 'id:' || p.id) AS k, s.people FROM takeout_sidecars s
           JOIN photos p ON p.id = s.photo_id WHERE s.people IS NOT NULL AND s.people != '[]'""").fetchall()
    names_of: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        try:
            names_of[r["k"]].update(json.loads(r["people"]))
        except ValueError:
            continue
    if not names_of:
        return []
    photos_with_name: Counter = Counter()
    for names in names_of.values():
        photos_with_name.update(names)

    dismissed = set(json.loads(db.get_meta(conn, "takeout_names_dismissed", "[]") or "[]"))
    taken_names = {r[0].lower() for r in conn.execute(
        "SELECT name FROM persons WHERE name IS NOT NULL AND merged_into IS NULL")}
    cands = []
    for person in conn.execute(
            "SELECT id, display_no, cover_face_id, photo_count FROM persons "
            "WHERE name IS NULL AND merged_into IS NULL AND ignored = 0 AND photo_count >= ?", (min_photos,)):
        keys = {r[0] for r in conn.execute(
            """SELECT DISTINCT COALESCE(p.sha256, 'id:' || p.id) FROM faces f JOIN photos p ON p.id = f.photo_id
               WHERE f.person_id = ? AND p.status = 'ok'""", (person["id"],))}
        labelled = [k for k in keys if k in names_of]
        if len(labelled) < min_photos:
            continue
        counts = Counter(n for k in labelled for n in names_of[k])
        for name, c in counts.most_common(3):
            share, coverage = c / len(labelled), c / photos_with_name[name]
            if c < min_photos or share < min_share or coverage < min_coverage:
                continue
            if name.lower() in taken_names or f"{person['id']}:{name}" in dismissed:
                continue
            cands.append((share * coverage, c, person, name, len(labelled), photos_with_name[name]))
    cands.sort(key=lambda x: (-x[0], -x[1]))
    used_people, used_names, out = set(), set(), []
    for score, c, person, name, labelled, total in cands:
        if person["id"] in used_people or name in used_names:
            continue
        used_people.add(person["id"])
        used_names.add(name)
        out.append({"person_id": person["id"], "name": name, "cover_face_id": person["cover_face_id"],
                    "label": f"Person {person['display_no']:03d}" if person["display_no"] else f"Person {person['id']}",
                    "matched_photos": c, "labelled_photos": labelled, "photos_with_name": total,
                    "score": round(score, 3)})
    return out


def dismiss_name_suggestion(conn: sqlite3.Connection, person_id: int, name: str) -> None:
    dismissed = set(json.loads(db.get_meta(conn, "takeout_names_dismissed", "[]") or "[]"))
    dismissed.add(f"{person_id}:{name}")
    db.set_meta(conn, "takeout_names_dismissed", json.dumps(sorted(dismissed)))
    db.audit(conn, "takeout_name_dismissed", "person", person_id, {"name": name})
    conn.commit()
