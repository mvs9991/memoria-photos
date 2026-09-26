"""Export what Memoria knows to XMP sidecars that other photo tools read.

People (as keywords, a People|Name hierarchy, and MWG face regions), your tags, your
star rating, descriptions, and corrected dates/places go into one `<file>.<ext>.xmp`
per photo — the naming darktable, digiKam and Lightroom (with sidecar import) accept.

Sidecars are written to a separate export folder that mirrors the library's layout;
a folder inside a photo root is refused, because Memoria never writes next to originals.
Copy them beside the photos yourself if the other tool needs them there.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from xml.sax.saxutils import escape

from ..metadata import ts_to_naive

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "stDim": "http://ns.adobe.com/xap/1.0/sType/Dimensions#",
    "stArea": "http://ns.adobe.com/xmp/sType/Area#",
}
AUTO_TAG_MIN_SCORE = 2.5      # automatic tags exported only where they are high-precision


class ExportError(ValueError):
    pass


def _check_destination(conn: sqlite3.Connection, out: Path) -> Path:
    out = out.resolve()
    for (root,) in conn.execute("SELECT path FROM roots"):
        r = Path(root).resolve()
        if out == r or r in out.parents:
            raise ExportError(f"{out} is inside the photo folder {r}; choose a folder outside your library")
    return out


def _load(conn: sqlite3.Connection, include_auto_tags: bool, photo_ids: list[int] | None = None):
    """Rows, tags and named faces for XMP: every analysed photo, or just `photo_ids`."""
    sql = """SELECT p.id, p.root_id, p.rel_path, p.width, p.height, p.rating, p.favorite, p.description,
                    p.taken_ts, p.date_source, p.gps_lat, p.gps_lon, p.location_source, p.caption,
                    pl.name AS place, pl.city, pl.admin1, pl.country
             FROM photos p LEFT JOIN places pl ON pl.id = p.place_id WHERE p.status = 'ok'"""
    rows = []
    if photo_ids is None:
        rows = conn.execute(sql).fetchall()
    else:
        for i in range(0, len(photo_ids), 900):
            chunk = photo_ids[i:i + 900]
            rows += conn.execute(sql + f" AND p.id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    tags_of: dict[int, list[str]] = {}
    min_score = AUTO_TAG_MIN_SCORE if include_auto_tags else 5.0   # user tags score 10
    for pid, name in conn.execute(
            "SELECT pt.photo_id, t.name FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.score >= ?",
            (min_score,)):
        tags_of.setdefault(int(pid), []).append(name)
    faces_of: dict[int, list] = {}
    for f in conn.execute(
            """SELECT f.photo_id, f.x1, f.y1, f.x2, f.y2, pe.name FROM faces f JOIN persons pe ON pe.id = f.person_id
               WHERE pe.name IS NOT NULL AND pe.merged_into IS NULL"""):
        faces_of.setdefault(int(f["photo_id"]), []).append(f)
    return rows, tags_of, faces_of


def _sidecar(r, tags_of, faces_of) -> tuple[str, bool]:
    """-> (xml, whether the photo carries anything the user added)."""
    pid = int(r["id"])
    faces = faces_of.get(pid, [])
    tags = sorted(set(tags_of.get(pid, [])))
    corrected_date = r["date_source"] == "user"
    corrected_place = r["location_source"] == "user"
    has_user_data = bool(faces or tags or r["rating"] or r["description"] or corrected_date or corrected_place)
    return _render(r, faces, tags, corrected_date, corrected_place), has_user_data


def sidecars_for(conn: sqlite3.Connection, photo_ids: list[int], include_auto_tags: bool = False) -> dict[int, str]:
    """XMP text for each of `photo_ids` (used when exporting copies of the photos themselves)."""
    rows, tags_of, faces_of = _load(conn, include_auto_tags, photo_ids)
    return {int(r["id"]): _sidecar(r, tags_of, faces_of)[0] for r in rows}


def refuse_inside_roots(conn: sqlite3.Connection, folders) -> None:
    """Every folder an export writes to must be outside every photo root. Checking only the chosen
    folder is not enough: exporting into a root's *parent* with the original layout (or XMP's
    mirror of it) would recreate the root's own path and write next to the originals."""
    roots = [Path(r[0]).resolve() for r in conn.execute("SELECT path FROM roots")]
    for f in {Path(f).resolve() for f in folders}:
        for r in roots:
            if f == r or r in f.parents:
                raise ExportError(f"{f} would be inside the photo folder {r}; choose another export folder")


def export_xmp(conn: sqlite3.Connection, out_dir: str | Path, include_auto_tags: bool = False,
               everything: bool = False) -> dict:
    out = _check_destination(conn, Path(out_dir))
    t0 = time.time()
    roots = {int(r[0]): Path(r[1]).name or f"root{r[0]}" for r in conn.execute("SELECT id, path FROM roots")}
    refuse_inside_roots(conn, [out / name for name in roots.values()])
    rows, tags_of, faces_of = _load(conn, include_auto_tags)
    written = 0
    for r in rows:
        xml, has_user_data = _sidecar(r, tags_of, faces_of)
        if not (has_user_data or everything):
            continue
        dest = out / roots.get(r["root_id"], "library") / (r["rel_path"] + ".xmp")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(xml, encoding="utf-8")
        os.replace(tmp, dest)
        written += 1
    (out / "memoria-export.json").write_text(json.dumps({
        "exported_at": time.time(), "sidecars": written, "auto_tags": include_auto_tags}, indent=2), encoding="utf-8")
    return {"written": written, "folder": str(out), "seconds": round(time.time() - t0, 2)}


def _bag(items: list[str]) -> str:
    return "<rdf:Bag>" + "".join(f"<rdf:li>{escape(i)}</rdf:li>" for i in items) + "</rdf:Bag>"


def _render(r, faces, tags, corrected_date: bool, corrected_place: bool) -> str:
    names = sorted({f["name"] for f in faces})
    attrs = []
    if r["rating"]:
        attrs.append(f'xmp:Rating="{int(r["rating"])}"')
    if corrected_date and r["taken_ts"] is not None:
        attrs.append(f'exif:DateTimeOriginal="{ts_to_naive(r["taken_ts"]).strftime("%Y-%m-%dT%H:%M:%S")}"')
    if corrected_place and r["gps_lat"] is not None:
        attrs.append(f'exif:GPSLatitude="{_dms(r["gps_lat"], "NS")}"')
        attrs.append(f'exif:GPSLongitude="{_dms(r["gps_lon"], "EW")}"')
    for key, col in (("photoshop:City", "city"), ("photoshop:State", "admin1"), ("photoshop:Country", "country")):
        if r[col]:
            attrs.append(f'{key}="{escape(r[col], {chr(34): "&quot;"})}"')
    body = []
    keywords = tags + names
    if keywords:
        body.append(f"<dc:subject>{_bag(keywords)}</dc:subject>")
        hierarchy = [f"People|{n}" for n in names] + [f"Memoria|{t}" for t in tags]
        body.append(f"<lr:hierarchicalSubject>{_bag(hierarchy)}</lr:hierarchicalSubject>")
    if r["description"]:
        body.append('<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
                    f'{escape(r["description"])}</rdf:li></rdf:Alt></dc:description>')
    if faces and r["width"] and r["height"]:
        regions = []
        for f in faces:
            x, y = (f["x1"] + f["x2"]) / 2, (f["y1"] + f["y2"]) / 2
            w, h = f["x2"] - f["x1"], f["y2"] - f["y1"]
            regions.append(
                '<rdf:li rdf:parseType="Resource">'
                f'<mwg-rs:Name>{escape(f["name"])}</mwg-rs:Name><mwg-rs:Type>Face</mwg-rs:Type>'
                f'<mwg-rs:Area stArea:x="{x:.5f}" stArea:y="{y:.5f}" stArea:w="{w:.5f}" stArea:h="{h:.5f}" '
                'stArea:unit="normalized"/></rdf:li>')
        body.append('<mwg-rs:Regions rdf:parseType="Resource">'
                    f'<mwg-rs:AppliedToDimensions stDim:w="{r["width"]}" stDim:h="{r["height"]}" stDim:unit="pixel"/>'
                    f'<mwg-rs:RegionList><rdf:Bag>{"".join(regions)}</rdf:Bag></mwg-rs:RegionList>'
                    '</mwg-rs:Regions>')
    ns = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items() if k not in ("x", "rdf"))
    return ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            f'<x:xmpmeta xmlns:x="{NS["x"]}" x:xmptk="Memoria">\n'
            f' <rdf:RDF xmlns:rdf="{NS["rdf"]}">\n'
            f'  <rdf:Description rdf:about="" {ns} '
            f'{" ".join(attrs)}>\n   ' + "\n   ".join(body) + "\n  </rdf:Description>\n </rdf:RDF>\n</x:xmpmeta>\n"
            '<?xpacket end="w"?>\n')


def _dms(value: float, hemis: str) -> str:
    """XMP GPS form: 'DD,MM.mmmmN'."""
    ref = hemis[0] if value >= 0 else hemis[1]
    v = abs(value)
    d = int(v)
    return f"{d},{(v - d) * 60:.6f}{ref}"
