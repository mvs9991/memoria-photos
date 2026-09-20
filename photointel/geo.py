"""Offline reverse geocoding using GeoNames (no coordinates ever leave the machine).

Data files (downloaded once into <data>/geo): cities500.txt, admin1CodesASCII.txt,
admin2Codes.txt, countryInfo.txt. Optional: landmarks.tsv (see scripts/fetch_geodata.py).
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

EARTH_R_KM = 6371.0088
GEONAMES_BASE = "https://download.geonames.org/export/dump/"


@dataclass
class GeoPlace:
    geoname_id: int
    name: str
    lat: float
    lon: float
    population: int
    feature_code: str
    country_code: str
    admin1: str | None
    admin2: str | None
    country: str | None


@dataclass
class GeoResult:
    locality: GeoPlace | None     # most specific populated place (neighbourhood / village)
    city: GeoPlace | None         # the settlement that "contains" the point
    landmark: GeoPlace | None
    distance_km: float


def _xyz(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    la, lo = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=-1)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def _chord_for_km(km: float) -> float:
    return 2 * math.sin(km / (2 * EARTH_R_KM))


def city_radius_km(population: int, feature_code: str) -> float:
    """Rough "how far does this settlement reach" radius, from its population."""
    pop = max(population, 1)
    r = 2.5 + 6.0 * max(0.0, math.log10(pop / 10_000))
    if feature_code in ("PPLC", "PPLA"):
        r += 2.0
    return min(r, 35.0)


# Administrative seats are more likely to be the name a person would use.
FEATURE_BONUS = {"PPLC": 0.7, "PPLA": 0.5, "PPLA2": 0.35, "PPLA3": 0.2, "PPLA4": 0.1}


def city_score(population: int, feature_code: str, distance_km: float) -> float:
    """Rank candidate settlements for "which city is this photo in?".

    Population alone is unreliable: GeoNames sometimes attaches a metro population
    to a small outlying locality, which would then beat the real city centre from
    10+ km away. Distance is therefore measured relative to the settlement's own
    reach, and administrative seats get a small bonus.
    """
    pop = max(population, 1)
    radius = city_radius_km(pop, feature_code)
    return math.log10(pop + 1) - 1.2 * (distance_km / max(radius, 1e-6)) + FEATURE_BONUS.get(feature_code, 0.0)


class ReverseGeocoder:
    _instance: "ReverseGeocoder | None" = None
    _lock = threading.Lock()

    def __init__(self, geo_dir: Path):
        self.geo_dir = Path(geo_dir)
        self._load()

    @classmethod
    def get(cls, geo_dir: Path) -> "ReverseGeocoder":
        with cls._lock:
            if cls._instance is None or cls._instance.geo_dir != Path(geo_dir):
                cls._instance = cls(geo_dir)
            return cls._instance

    @staticmethod
    def available(geo_dir: Path) -> bool:
        return (Path(geo_dir) / "cities500.txt").exists() or (Path(geo_dir) / "cities_cache.npz").exists()

    # -- loading ------------------------------------------------------------------------
    def _load(self) -> None:
        cache = self.geo_dir / "cities_cache.npz"
        names_cache = self.geo_dir / "cities_cache.json"
        src = self.geo_dir / "cities500.txt"
        extra_file = self.geo_dir / "extra_places.tsv"
        newest_src = max([p.stat().st_mtime for p in (src, extra_file) if p.exists()] or [0])
        if cache.exists() and names_cache.exists() and cache.stat().st_mtime >= newest_src:
            arr = np.load(cache)
            meta = json.loads(names_cache.read_text(encoding="utf-8"))
        else:
            if not src.exists():
                raise FileNotFoundError(f"GeoNames data missing in {self.geo_dir}; run `photointel geo-setup`")
            arr, meta = self._parse(src)
            extra = self.geo_dir / "extra_places.tsv"
            if extra.exists():
                arr, meta = self._merge_extra(arr, meta, extra)
            np.savez(cache, **arr)
            names_cache.write_text(json.dumps(meta), encoding="utf-8")
        self.ids = arr["ids"]
        self.lat = arr["lat"]
        self.lon = arr["lon"]
        self.pop = arr["pop"]
        self.names: list[str] = meta["names"]
        self.fcodes: list[str] = meta["fcodes"]
        self.ccodes: list[str] = meta["ccodes"]
        self.a1: list[str] = meta["a1"]
        self.a2: list[str] = meta["a2"]
        self.tree = cKDTree(_xyz(self.lat, self.lon))
        self.admin1 = self._read_admin(self.geo_dir / "admin1CodesASCII.txt")
        self.admin2 = self._read_admin(self.geo_dir / "admin2Codes.txt")
        self.countries = self._read_countries(self.geo_dir / "countryInfo.txt")
        self.landmarks = self._load_landmarks(self.geo_dir / "landmarks.tsv")
        # Name index for text search ("Hyderabad", "Goa", "Tirupati").
        self._name_index: dict[str, list[int]] | None = None

    @staticmethod
    def _parse(src: Path):
        ids, lat, lon, pop = [], [], [], []
        names, fcodes, ccodes, a1, a2 = [], [], [], [], []
        with open(src, encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) < 15 or c[6] != "P":
                    continue
                if c[7] in ("PPLH", "PPLQ", "PPLW"):  # historical / abandoned / destroyed
                    continue
                ids.append(int(c[0]))
                names.append(c[1])
                lat.append(float(c[4]))
                lon.append(float(c[5]))
                fcodes.append(c[7])
                ccodes.append(c[8])
                a1.append(c[10])
                a2.append(c[11])
                pop.append(int(c[14] or 0))
        arr = {
            "ids": np.array(ids, dtype=np.int64),
            "lat": np.array(lat, dtype=np.float64),
            "lon": np.array(lon, dtype=np.float64),
            "pop": np.array(pop, dtype=np.int64),
        }
        return arr, {"names": names, "fcodes": fcodes, "ccodes": ccodes, "a1": a1, "a2": a2}


    @staticmethod
    def _merge_extra(arr: dict, meta: dict, extra: Path):
        """Merge per-country enrichment (every village) into the populated-place index."""
        known = set(arr["ids"].tolist())
        ids, lat, lon, pop = [], [], [], []
        names, fcodes, ccodes, a1, a2 = [], [], [], [], []
        with open(extra, encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) < 9:
                    continue
                try:
                    gid = int(c[0])
                except ValueError:
                    continue
                if gid in known:
                    continue
                known.add(gid)
                ids.append(gid)
                names.append(c[1])
                lat.append(float(c[2]))
                lon.append(float(c[3]))
                fcodes.append(c[4])
                ccodes.append(c[5])
                a1.append(c[6])
                a2.append(c[7])
                pop.append(int(c[8] or 0))
        if not ids:
            return arr, meta
        arr = {
            "ids": np.concatenate([arr["ids"], np.array(ids, dtype=np.int64)]),
            "lat": np.concatenate([arr["lat"], np.array(lat, dtype=np.float64)]),
            "lon": np.concatenate([arr["lon"], np.array(lon, dtype=np.float64)]),
            "pop": np.concatenate([arr["pop"], np.array(pop, dtype=np.int64)]),
        }
        meta = {"names": meta["names"] + names, "fcodes": meta["fcodes"] + fcodes,
                "ccodes": meta["ccodes"] + ccodes, "a1": meta["a1"] + a1, "a2": meta["a2"] + a2}
        return arr, meta

    @staticmethod
    def _read_admin(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not path.exists():
            return out
        with open(path, encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) >= 2:
                    out[c[0]] = c[1]
        return out

    @staticmethod
    def _read_countries(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not path.exists():
            return out
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                c = line.rstrip("\n").split("\t")
                if len(c) > 4:
                    out[c[0]] = c[4]
        return out

    def _load_landmarks(self, path: Path):
        if not path.exists():
            return None
        rows = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) >= 7:
                    rows.append(c)
        if not rows:
            return None
        lat = np.array([float(r[2]) for r in rows])
        lon = np.array([float(r[3]) for r in rows])
        return {"rows": rows, "tree": cKDTree(_xyz(lat, lon))}

    # -- queries ------------------------------------------------------------------------
    def _place(self, i: int) -> GeoPlace:
        cc = self.ccodes[i]
        return GeoPlace(
            geoname_id=int(self.ids[i]), name=self.names[i], lat=float(self.lat[i]), lon=float(self.lon[i]),
            population=int(self.pop[i]), feature_code=self.fcodes[i], country_code=cc,
            admin1=self.admin1.get(f"{cc}.{self.a1[i]}"),
            admin2=self.admin2.get(f"{cc}.{self.a1[i]}.{self.a2[i]}"),
            country=self.countries.get(cc, cc),
        )

    def lookup(self, lat: float, lon: float) -> GeoResult:
        q = _xyz(np.array([lat]), np.array([lon]))[0]
        dist, idx = self.tree.query(q, k=1)
        near_km = 2 * EARTH_R_KM * math.asin(min(1.0, dist / 2))
        locality = self._place(int(idx)) if near_km <= 15 else None

        # City: the best-scoring settlement whose reach covers the point.
        cand = self.tree.query_ball_point(q, _chord_for_km(35.0))
        best, best_score = None, -1e9
        fallback, fallback_d = None, 1e9
        for i in cand:
            d = haversine_km(lat, lon, self.lat[i], self.lon[i])
            p = int(self.pop[i])
            if d <= city_radius_km(p, self.fcodes[i]):
                sc = city_score(p, self.fcodes[i], d)
                if sc > best_score:
                    best, best_score = i, sc
            if p >= 5000 and d < fallback_d:
                fallback, fallback_d = i, d
        if best is None and fallback is not None and fallback_d <= 25:
            best = fallback
        city = self._place(best) if best is not None else locality
        if city is None and near_km <= 60:
            city = self._place(int(idx))
        if city is not None and locality is not None and city.geoname_id != locality.geoname_id:
            city_dist = haversine_km(lat, lon, city.lat, city.lon)
            reach = city_radius_km(city.population, city.feature_code)
            # Between two small villages, the nearer name is the one a person would use.
            if (near_km <= 3.0 and city.population < 20_000
                    and city.feature_code not in ("PPLC", "PPLA", "PPLA2", "PPLA3")
                    and city_dist > near_km):
                city = locality
            # A named town at the very edge of a bigger town's reach is its own place,
            # not a neighbourhood of it (Calangute is not part of Panjim).
            elif (locality.population >= 3_000 and city_dist / max(reach, 1e-6) > 0.75
                  and city.population < 300_000):
                city = locality

        landmark = None
        if self.landmarks is not None:
            k = min(5, len(self.landmarks["rows"]))
            lds, lis = self.landmarks["tree"].query(q, k=k)
            for ld, li in zip(np.atleast_1d(lds), np.atleast_1d(lis)):
                r = self.landmarks["rows"][int(li)]
                lkm = 2 * EARTH_R_KM * math.asin(min(1.0, float(ld) / 2))
                # Buildings need a tight radius; natural features (beach, peak, valley) are large.
                limit = 1.5 if (len(r) > 7 and r[7] in ("T", "H", "L")) else 0.5
                if lkm <= limit:
                    # A "landmark" that just repeats the town name adds nothing.
                    if r[1] in {getattr(city, "name", None), getattr(locality, "name", None)}:
                        continue
                    landmark = GeoPlace(int(r[0]), r[1], float(r[2]), float(r[3]), 0, r[4], r[5],
                                        self.admin1.get(f"{r[5]}.{r[6]}"), None, self.countries.get(r[5], r[5]))
                    break
        return GeoResult(locality=locality, city=city, landmark=landmark, distance_km=near_km)

    def find_by_name(self, name: str, min_population: int = 20_000, limit: int = 5) -> list[GeoPlace]:
        """Case-insensitive exact name match — used for folder-name location hints."""
        if self._name_index is None:
            idx: dict[str, list[int]] = {}
            for i, n in enumerate(self.names):
                if int(self.pop[i]) >= 1000:
                    idx.setdefault(n.lower(), []).append(i)
            self._name_index = idx
        hits = self._name_index.get(name.lower(), [])
        hits = [i for i in hits if int(self.pop[i]) >= min_population]
        hits.sort(key=lambda i: -int(self.pop[i]))
        return [self._place(i) for i in hits[:limit]]
