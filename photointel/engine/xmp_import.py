"""Read XMP sidecars that other photo apps wrote (Lightroom, digiKam, darktable, …).

For someone arriving with years of work in another app: star ratings, keywords,
descriptions and the names on face regions come across. As with Google Takeout:

* **Applied once per sidecar version**, so a change made in Memoria is not undone. Stars and
  a description only fill what is empty here; keywords become your own tags, but only those
  new since the sidecar was last read (a tag removed here does not come back).
* **Names are never applied to faces.** A region says someone is in the photo, not which of
  Memoria's faces is them; the names only feed the same name *suggestions* as Takeout, for
  the user to accept.
* Sidecars Memoria exported itself (x:xmptk="Memoria") are skipped: their keywords include
  automatic tags, which must not come back as the user's.

Which file: `<name>.<ext>.xmp` (digiKam, darktable), else `<stem>.xmp` (Lightroom). When a
RAW and a JPEG share a stem, a stem sidecar belongs to the RAW, which is what Lightroom
writes it for.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

from .. import db
from ..config import RAW_EXTENSIONS
from . import albums as albums_mod

log = logging.getLogger(__name__)

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "MP": "http://ns.microsoft.com/photo/1.2/",
    "MPRI": "http://ns.microsoft.com/photo/1.2/t/RegionInfo#",
    "MPReg": "http://ns.microsoft.com/photo/1.2/t/Region#",
}
PEOPLE_ROOTS = ("people", "persons", "person", "faces", "names")


def _q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


def parse_sidecar(data: bytes) -> dict | None:
    """-> {rating, keywords, description, people, tool} or None when it is not XMP."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    tool = root.get(_q("x", "xmptk")) or ""
    rating = 0
    keywords: list[str] = []
    hier: list[str] = []
    description = None
    people: list[str] = []
    for desc in root.iter(_q("rdf", "Description")):
        r = desc.get(_q("xmp", "Rating"))
        if r is None:
            el = desc.find(_q("xmp", "Rating"))
            r = el.text if el is not None else None
        try:
            if r is not None:
                rating = max(rating, min(5, max(0, int(float(r)))))
        except ValueError:
            pass
        for li in desc.findall(f"{_q('dc', 'subject')}//{_q('rdf', 'li')}"):
            if li.text and li.text.strip():
                keywords.append(li.text.strip())
        for li in desc.findall(f"{_q('lr', 'hierarchicalSubject')}//{_q('rdf', 'li')}"):
            if li.text and li.text.strip():
                hier.append(li.text.strip())
        d = desc.find(f"{_q('dc', 'description')}//{_q('rdf', 'li')}")
        if d is not None and d.text and d.text.strip():
            description = d.text.strip()
    # face regions: MWG (Lightroom, digiKam, darktable) and Microsoft's
    for el in root.iter():
        if el.tag == _q("mwg-rs", "Name") and el.text:
            people.append(el.text.strip())
        name = el.get(_q("mwg-rs", "Name"))
        if name and el.get(_q("mwg-rs", "Type"), "Face") == "Face":
            people.append(name.strip())
        if el.tag == _q("MPReg", "PersonDisplayName") and el.text:
            people.append(el.text.strip())
        pdn = el.get(_q("MPReg", "PersonDisplayName"))
        if pdn:
            people.append(pdn.strip())
    # "People|Priya" in the hierarchy names a person, not a subject
    for h in hier:
        parts = [p.strip() for p in h.split("|") if p.strip()]
        if len(parts) >= 2 and parts[0].lower() in PEOPLE_ROOTS:
            people.append(parts[-1])
    people = list(dict.fromkeys(p for p in people if p))
    lowered = {p.lower() for p in people}
    keywords = [k for k in dict.fromkeys(keywords) if k.lower() not in lowered]
    return {"rating": rating, "keywords": keywords, "description": description, "people": people, "tool": tool}


def _sidecar_for(filename: str, names: set[str], siblings_by_stem: dict[str, list[str]]) -> str | None:
    lower = {n.lower(): n for n in names}
    exact = lower.get(f"{filename.lower()}.xmp")
    if exact:
        return exact
    stem, ext = os.path.splitext(filename)
    by_stem = lower.get(f"{stem.lower()}.xmp")
    if not by_stem:
        return None
    others = siblings_by_stem.get(stem.lower(), [])
    if len(others) > 1:           # RAW + JPEG sharing a stem: Lightroom's sidecar is the RAW's
        return by_stem if ext.lower() in RAW_EXTENSIONS else None
    return by_stem


def import_xmp(ctx, conn: sqlite3.Connection) -> dict:
    t0 = time.time()
    stats: Counter = Counter()
    folders = conn.execute("SELECT DISTINCT p.root_id, r.path AS root, p.folder FROM photos p JOIN roots r "
                           "ON r.id = p.root_id WHERE p.status = 'ok'").fetchall()
    known = {int(r["photo_id"]): r for r in conn.execute("SELECT * FROM xmp_sidecars")}
    for f in folders:
        abs_dir = os.path.join(f["root"], f["folder"])
        try:
            names = {e.name for e in os.scandir(abs_dir) if e.is_file() and e.name.lower().endswith(".xmp")}
        except OSError:
            continue
        if not names:
            continue
        photos = conn.execute("SELECT id, filename, rating, description FROM photos WHERE root_id = ? AND folder = ? "
                              "AND status = 'ok'", (f["root_id"], f["folder"])).fetchall()
        by_stem: dict[str, list[str]] = defaultdict(list)
        for p in photos:
            by_stem[os.path.splitext(p["filename"])[0].lower()].append(p["filename"])
        for p in photos:
            side = _sidecar_for(p["filename"], names, by_stem)
            if side is None:
                continue
            path = os.path.join(abs_dir, side)
            try:
                mtime = os.path.getmtime(path)
                prev = known.get(int(p["id"]))
                if prev is not None and abs(prev["mtime"] - mtime) < 1:
                    continue                                    # this version was read already
                with open(path, "rb") as fh:
                    parsed = parse_sidecar(fh.read())
            except OSError:
                stats["unreadable"] += 1
                continue
            if parsed is None:
                stats["not_xmp"] += 1
                continue
            if parsed["tool"].strip().lower().startswith("memoria"):
                stats["memoria_exports_skipped"] += 1
                continue
            _apply(conn, p, parsed, prev, stats)
            conn.execute(
                """INSERT INTO xmp_sidecars(photo_id, path, mtime, rating, keywords, description, people, imported_at)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(photo_id) DO UPDATE SET path=excluded.path, mtime=excluded.mtime,
                   rating=excluded.rating, keywords=excluded.keywords, description=excluded.description,
                   people=excluded.people, imported_at=excluded.imported_at""",
                (int(p["id"]), os.path.join(f["folder"], side), mtime, parsed["rating"], json.dumps(parsed["keywords"]),
                 parsed["description"], json.dumps(parsed["people"]), time.time()))
            stats["sidecars"] += 1
        conn.commit()
    out = dict(stats)
    out["seconds"] = round(time.time() - t0, 2)
    if stats["sidecars"]:
        db.bump_generation(conn, "people")
        conn.commit()
        log.info("XMP import: %s", out)
    return out


def _apply(conn: sqlite3.Connection, p, parsed: dict, prev, stats: Counter) -> None:
    pid = int(p["id"])
    if parsed["rating"] and not p["rating"]:
        conn.execute("UPDATE photos SET rating = ? WHERE id = ?", (parsed["rating"], pid))
        stats["ratings"] += 1
    if parsed["description"] and not p["description"]:
        conn.execute("UPDATE photos SET description = ? WHERE id = ?", (parsed["description"], pid))
        stats["descriptions"] += 1
    seen = set(json.loads(prev["keywords"])) if prev is not None and prev["keywords"] else set()
    for kw in parsed["keywords"]:
        if kw in seen:
            continue                           # read before; if the user removed it here, it stays removed
        albums_mod.add_user_tag(conn, [pid], kw)
        stats["keywords"] += 1


def people_by_content(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(content key, JSON list of names) rows, for the name suggestions."""
    return conn.execute(
        """SELECT COALESCE(p.sha256, 'id:' || p.id) AS k, s.people FROM xmp_sidecars s
           JOIN photos p ON p.id = s.photo_id WHERE s.people IS NOT NULL AND s.people != '[]'""").fetchall()

