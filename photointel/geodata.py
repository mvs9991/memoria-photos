"""Download and prepare offline GeoNames data.

Base data (cities500, admin codes, country info) covers the world's populated
places above ~500 inhabitants. Optional per-country enrichment adds every
village and a curated set of landmarks (temples, forts, beaches, peaks, parks…),
which matters a lot outside big cities.

Only public reference data is downloaded — no photo or location data is ever
sent anywhere.
"""
from __future__ import annotations

import io
import logging
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

BASE = "https://download.geonames.org/export/dump/"
BASE_FILES = ["cities500.zip", "admin1CodesASCII.txt", "admin2Codes.txt", "countryInfo.txt"]

# Landmark feature codes worth surfacing in a photo library.
LANDMARK_CODES = {
    # man-made
    "MNMT", "TMPL", "CH", "MSQE", "SHRN", "PAL", "CSTL", "FT", "MUS", "ZOO", "THTR", "STDM", "AIRP",
    "UNIV", "TOWR", "GDN", "AMUS", "HSTS", "RUIN", "ARCH", "PYR", "OBS", "LTHSE", "BDG", "MAR", "RSRT",
    # natural / area
    "BCH", "MT", "PK", "VAL", "ISL", "LK", "FLLS", "BAY", "CAVE", "CNYN", "GLCR", "HLL", "SPNG", "DSRT",
    "PRK", "RESN", "RESF", "RESW",
}
NATURAL_CLASSES = {"T", "H", "L"}


def _download(url: str, dest: Path, timeout: int = 180, prefer: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Memoria/0.1 (personal photo library)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            inner = [n for n in z.namelist() if n.endswith(".txt") and "readme" not in n.lower()]
            if prefer:
                inner = [n for n in inner if Path(n).name.lower() == prefer.lower()] or inner
            if not inner:
                raise RuntimeError(f"no data .txt inside {url}")
            dest.write_bytes(z.read(inner[0]))
    else:
        dest.write_bytes(data)
    return dest


def fetch_base(geo_dir: Path, force: bool = False) -> dict:
    geo_dir = Path(geo_dir)
    out = {}
    for name in BASE_FILES:
        target = geo_dir / (name.replace(".zip", ".txt") if name.endswith(".zip") else name)
        if target.exists() and not force:
            out[target.name] = "present"
            continue
        _download(BASE + name, target, prefer=target.name)
        out[target.name] = f"{target.stat().st_size:,} bytes"
    return out


def fetch_countries(geo_dir: Path, countries: list[str], force: bool = False) -> dict:
    """Add all populated places and curated landmarks for the given ISO country codes."""
    geo_dir = Path(geo_dir)
    extra_path = geo_dir / "extra_places.tsv"
    landmark_path = geo_dir / "landmarks.tsv"
    done = set()
    marker = geo_dir / "countries.txt"
    if marker.exists() and not force:
        done = {c.strip() for c in marker.read_text(encoding="utf-8").split() if c.strip()}
    todo = [c.upper() for c in countries if c.upper() not in done]
    if not todo:
        return {"status": "up to date", "countries": sorted(done)}

    places_out = open(extra_path, "a", encoding="utf-8")
    land_out = open(landmark_path, "a", encoding="utf-8")
    stats = {}
    try:
        for cc in todo:
            tmp = geo_dir / f"{cc}.txt"
            try:
                _download(f"{BASE}{cc}.zip", tmp, timeout=600, prefer=f"{cc}.txt")
            except Exception as exc:
                log.warning("Could not download %s: %s", cc, exc)
                stats[cc] = f"failed: {exc}"
                continue
            n_place = n_land = 0
            with open(tmp, encoding="utf-8") as f:
                for line in f:
                    c = line.rstrip("\n").split("\t")
                    if len(c) < 15:
                        continue
                    fclass, fcode = c[6], c[7]
                    if fclass == "P":
                        if fcode in ("PPLH", "PPLQ", "PPLW"):
                            continue
                        places_out.write("\t".join([c[0], c[1], c[4], c[5], fcode, c[8], c[10], c[11], c[14] or "0"]) + "\n")
                        n_place += 1
                    elif fcode in LANDMARK_CODES:
                        land_out.write("\t".join([c[0], c[1], c[4], c[5], fcode, c[8], c[10], fclass]) + "\n")
                        n_land += 1
            tmp.unlink(missing_ok=True)
            done.add(cc)
            stats[cc] = {"places": n_place, "landmarks": n_land}
            log.info("Added %s: %d places, %d landmarks", cc, n_place, n_land)
    finally:
        places_out.close()
        land_out.close()
    marker.write_text("\n".join(sorted(done)), encoding="utf-8")
    # invalidate the compiled cache so the next lookup rebuilds it
    for f in ("cities_cache.npz", "cities_cache.json"):
        (geo_dir / f).unlink(missing_ok=True)
    return {"countries": sorted(done), "added": stats}


def countries_in_library(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT country_code FROM places WHERE country_code IS NOT NULL").fetchall()
    return [r[0] for r in rows if r[0]]
