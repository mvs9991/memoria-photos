"""Build a realistic synthetic photo library with ground truth.

Sources: COCO val2017 (scenes, CC-licensed Flickr photos) + LFW (faces).
The generator fabricates a plausible personal library: nested/odd folder names,
phone + camera + WhatsApp + screenshot + download provenance, EXIF dates and GPS
forming trips and events, duplicate families, bursts, and a pile of edge cases
(corrupt, truncated, mislabelled extension, unicode names, huge panorama, ...).

Ground truth is written to ground_truth.json for the evaluation scripts.

    python eval/make_test_library.py --out D:/pi_cache/testlib --photos 1800
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import piexif
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATASETS = Path(os.environ.get("PI_DATASETS", "D:/pi_cache/datasets"))
COCO_DIR = Path(os.environ.get("COCO_DIR", DATASETS / "val2017"))
COCO_CAPTIONS = Path(os.environ.get("COCO_CAPTIONS", "D:/pi_cache/datasets/annotations/captions_val2017.json"))
LFW_DIR = Path(os.environ.get("LFW_DIR", DATASETS / "lfw_funneled"))

PEOPLE = [
    ("Ghat", 1.00), ("Priya", 0.75), ("Ravi", 0.60), ("Lakshmi", 0.45), ("Kiran", 0.40),
    ("Anil", 0.30), ("Meena", 0.28), ("Suresh", 0.22), ("Divya", 0.18), ("Arjun", 0.15),
]

PLACES = {
    "ongole": (15.5057, 80.0499, "Ongole"),
    "hyderabad": (17.3850, 78.4867, "Hyderabad"),
    "charminar": (17.3616, 78.4747, "Hyderabad"),
    "golconda": (17.3833, 78.4011, "Hyderabad"),
    "vijayawada": (16.5062, 80.6480, "Vijayawada"),
    "tirupati": (13.6288, 79.4192, "Tirupati"),
    "goa": (15.5439, 73.7553, "Calangute"),
    "araku": (18.3273, 82.8752, "Araku Valley"),
    "bengaluru": (12.9716, 77.5946, "Bengaluru"),
    "vizag": (17.6868, 83.2185, "Visakhapatnam"),
}

CAMERAS = [
    ("samsung", "SM-S918B", "phone"),
    ("Apple", "iPhone 14 Pro", "phone"),
    ("Xiaomi", "M2101K6G", "phone"),
    ("Canon", "Canon EOS 200D", "camera"),
    ("NIKON CORPORATION", "NIKON D5600", "camera"),
]


@dataclass
class EventSpec:
    key: str
    title: str
    category: str
    place: str
    start: datetime
    days: int
    photos: int
    people: list[str]
    keywords: list[str]
    folder: str
    gps_fraction: float = 0.85
    source: str = "phone"


def event_plan() -> list[EventSpec]:
    return [
        EventSpec("hyd_trip_2025", "Hyderabad Trip", "monument", "charminar", datetime(2025, 8, 14, 9, 30), 3, 120,
                  ["Ghat", "Priya", "Ravi"], ["monument", "city", "street", "food", "market"], "DCIM/Camera"),
        EventSpec("wedding_vjw", "Wedding Vijayawada", "wedding", "vijayawada", datetime(2026, 1, 23, 8, 0), 2, 150,
                  ["Ghat", "Priya", "Ravi", "Lakshmi", "Kiran", "Anil"],
                  ["wedding", "crowd", "flowers", "food", "dining table"], "Wedding Jan 2026"),
        EventSpec("family_ongole", "Family Function Ongole", "family gathering", "ongole", datetime(2024, 3, 9, 11, 0),
                  1, 70, ["Ghat", "Lakshmi", "Meena", "Suresh"], ["group", "dining table", "food", "people"],
                  "DCIM/Camera"),
        EventSpec("goa_2023", "Goa Beach Trip", "beach", "goa", datetime(2023, 12, 26, 7, 45), 4, 130,
                  ["Ghat", "Kiran"], ["beach", "surf", "ocean", "boat", "sand"], "Trips/Goa 2023"),
        EventSpec("tirupati_2024", "Tirupati Temple Visit", "religious ceremony", "tirupati",
                  datetime(2024, 7, 20, 5, 30), 1, 45, ["Ghat", "Lakshmi", "Priya"],
                  ["temple", "crowd", "people"], "DCIM/Camera"),
        EventSpec("birthday_2022", "Birthday at home", "birthday", "ongole", datetime(2022, 5, 17, 19, 0), 1, 55,
                  ["Ghat", "Priya", "Meena", "Divya"], ["cake", "candles", "party", "people"], "DCIM/Camera"),
        EventSpec("birthday_2025", "Birthday at home", "birthday", "ongole", datetime(2025, 5, 17, 19, 30), 1, 48,
                  ["Ghat", "Priya", "Arjun"], ["cake", "party", "people"], "DCIM/Camera"),
        EventSpec("araku_2022", "Araku Trip", "mountains", "araku", datetime(2022, 10, 8, 8, 0), 2, 85,
                  ["Ghat", "Ravi", "Kiran"], ["mountain", "train", "forest", "valley"], "Trips/Araku"),
        EventSpec("blr_work", "Bengaluru Work Trip", "conference", "bengaluru", datetime(2025, 2, 11, 9, 0), 3, 60,
                  ["Ghat", "Arjun"], ["office", "laptop", "city", "bus"], "Work/BLR", source="phone"),
        EventSpec("vizag_2024", "Vizag Weekend", "beach", "vizag", datetime(2024, 11, 2, 6, 30), 2, 70,
                  ["Ghat", "Priya", "Divya"], ["beach", "ocean", "boat"], "DCIM/Camera"),
        EventSpec("home_2023", "Everyday at home", "home interior", "ongole", datetime(2023, 6, 3, 17, 0), 1, 40,
                  ["Ghat", "Meena"], ["dog", "kitchen", "living room", "cat"], "DCIM/Camera", gps_fraction=0.4),
    ]


def load_coco(rng: random.Random) -> dict[str, list[Path]]:
    with open(COCO_CAPTIONS, encoding="utf-8") as f:
        data = json.load(f)
    caps: dict[int, list[str]] = {}
    for a in data["annotations"]:
        caps.setdefault(a["image_id"], []).append(a["caption"].lower())
    files = {int(im["id"]): COCO_DIR / im["file_name"] for im in data["images"]}
    by_kw: dict[str, list[Path]] = {}
    all_files = []
    for img_id, path in files.items():
        if not path.exists():
            continue
        all_files.append(path)
        text = " ".join(caps.get(img_id, []))
        for kw in ("beach", "surf", "ocean", "boat", "sand", "mountain", "snow", "train", "forest", "valley",
                   "cake", "candles", "party", "people", "group", "crowd", "wedding", "flowers", "food",
                   "dining table", "temple", "city", "street", "market", "monument", "office", "laptop",
                   "bus", "dog", "cat", "kitchen", "living room"):
            if kw in text:
                by_kw.setdefault(kw, []).append(path)
    by_kw["__all__"] = all_files
    return by_kw


def load_lfw(rng: random.Random) -> dict[str, list[Path]]:
    dirs = [d for d in LFW_DIR.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: -len(list(d.glob("*.jpg"))))
    out = {}
    for (name, _), d in zip(PEOPLE, dirs[:len(PEOPLE)]):
        out[name] = sorted(d.glob("*.jpg"))
    return out


@dataclass
class Photo:
    rel_path: str  # always POSIX-style, relative to the library root
    taken: datetime
    place: str | None
    people: list[str]
    event: str | None
    source: str
    dup_group: str | None = None
    dup_kind: str | None = None
    corrupt: bool = False
    note: str = ""
    burst_group: str | None = None
    origin: str | None = None   # rel_path of the photo this one's pixels came from


class LibraryBuilder:
    def __init__(self, out: Path, seed: int = 7):
        self.out = out
        self.rng = random.Random(seed)
        self.np_rng = np.random.default_rng(seed)
        self.coco = load_coco(self.rng)
        self.lfw = load_lfw(self.rng)
        self.records: list[Photo] = []
        self.counter = 0
        self.face_usage = {name: 0 for name in self.lfw}
        self.used_scenes: set = set()

    # ---------------------------------------------------------------- image composition
    def _pick_scene(self, keywords: list[str]) -> Path:
        """Pick an unused COCO scene. Reusing one would create unintended duplicate
        families and make the duplicate ground truth ambiguous."""
        pool: list[Path] = []
        for kw in keywords:
            pool.extend(self.coco.get(kw, []))
        if not pool:
            pool = self.coco["__all__"]
        fresh = [p for p in pool if p not in self.used_scenes]
        if not fresh:
            fresh = [p for p in self.coco["__all__"] if p not in self.used_scenes] or pool
        choice = self.rng.choice(fresh)
        self.used_scenes.add(choice)
        return choice

    def _face_image(self, person: str) -> Image.Image:
        files = self.lfw[person]
        idx = self.face_usage[person] % len(files)
        self.face_usage[person] += 1
        return Image.open(files[idx]).convert("RGB")

    def compose(self, keywords: list[str], people: list[str], kind: str) -> Image.Image:
        scene = Image.open(self._pick_scene(keywords)).convert("RGB")
        w, h = scene.size
        target_w = self.rng.choice([1920, 2400, 3024, 4032])
        scale = target_w / w
        scene = scene.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        if kind == "scene" or not people:
            return scene
        W, H = scene.size
        if kind == "portrait":
            person = people[0]
            face = self._face_image(person)
            size = int(H * 0.62)
            face = face.resize((size, size), Image.Resampling.LANCZOS)
            bg = scene.filter(ImageFilter.GaussianBlur(radius=max(3, H // 120)))
            x = (W - size) // 2 + self.rng.randint(-W // 12, W // 12)
            y = int(H * 0.18)
            bg.paste(face, (max(0, x), max(0, y)))
            return bg
        # group photo: paste faces along a row in the lower half
        n = len(people)
        face_h = int(H * self.rng.uniform(0.22, 0.34))
        gap = int(face_h * 0.12)
        total = n * face_h + (n - 1) * gap
        x0 = max(0, (W - total) // 2)
        y0 = int(H * self.rng.uniform(0.32, 0.5))
        for i, person in enumerate(people):
            face = self._face_image(person).resize((face_h, face_h), Image.Resampling.LANCZOS)
            jitter = self.rng.randint(-face_h // 12, face_h // 12)
            scene.paste(face, (x0 + i * (face_h + gap), max(0, min(H - face_h, y0 + jitter))))
        return scene

    # ---------------------------------------------------------------- EXIF + saving
    def _exif(self, taken: datetime, place: str | None, camera: tuple, orientation: int = 1,
              software: str | None = None) -> bytes:
        make, model, _ = camera
        zeroth = {
            piexif.ImageIFD.Make: make.encode(), piexif.ImageIFD.Model: model.encode(),
            piexif.ImageIFD.Orientation: orientation,
            piexif.ImageIFD.DateTime: taken.strftime("%Y:%m:%d %H:%M:%S").encode(),
        }
        if software:
            zeroth[piexif.ImageIFD.Software] = software.encode()
        exif = {
            piexif.ExifIFD.DateTimeOriginal: taken.strftime("%Y:%m:%d %H:%M:%S").encode(),
            piexif.ExifIFD.DateTimeDigitized: taken.strftime("%Y:%m:%d %H:%M:%S").encode(),
            piexif.ExifIFD.OffsetTimeOriginal: b"+05:30",
            piexif.ExifIFD.FNumber: (18, 10),
            piexif.ExifIFD.ExposureTime: (1, self.rng.choice([60, 125, 250, 500])),
            piexif.ExifIFD.ISOSpeedRatings: self.rng.choice([50, 100, 200, 400, 800]),
            piexif.ExifIFD.FocalLength: (self.rng.choice([26, 35, 50, 85]), 1),
            piexif.ExifIFD.LensModel: b"Standard Lens",
        }
        gps = {}
        if place:
            lat, lon, _ = PLACES[place]
            lat += self.np_rng.normal(0, 0.004)
            lon += self.np_rng.normal(0, 0.004)
            gps = {
                piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
                piexif.GPSIFD.GPSLatitude: _deg_to_dms(abs(lat)),
                piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
                piexif.GPSIFD.GPSLongitude: _deg_to_dms(abs(lon)),
                piexif.GPSIFD.GPSAltitudeRef: 0,
                piexif.GPSIFD.GPSAltitude: (int(self.rng.uniform(5, 500) * 100), 100),
            }
        return piexif.dump({"0th": zeroth, "Exif": exif, "GPS": gps, "1st": {}, "thumbnail": None})

    def save(self, img: Image.Image, rel_path: str, taken: datetime, place: str | None, camera: tuple,
             quality: int = 90, exif: bool = True, orientation: int = 1, software: str | None = None,
             fmt: str = "JPEG") -> Path:
        dest = self.out / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        kwargs = {}
        if fmt == "JPEG":
            kwargs = {"quality": quality, "subsampling": 2}
            if exif:
                kwargs["exif"] = self._exif(taken, place, camera, orientation, software)
        elif fmt == "PNG":
            kwargs = {"compress_level": 6}
        elif fmt == "WEBP":
            kwargs = {"quality": quality}
        elif fmt == "TIFF":
            kwargs = {"compression": "tiff_lzw"}
        img.save(dest, fmt, **kwargs)
        try:  # Windows cannot set mtimes at/below the epoch in local time
            ts = taken.timestamp()
            os.utime(dest, (ts, ts))
        except (OSError, OverflowError, ValueError):
            pass
        return dest

    def next_name(self, source: str, taken: datetime, ext: str = "jpg") -> str:
        self.counter += 1
        stamp = taken.strftime("%Y%m%d_%H%M%S")
        if source == "phone":
            return f"IMG_{stamp}.{ext}"
        if source == "pixel":
            return f"PXL_{taken.strftime('%Y%m%d_%H%M%S')}{self.counter % 1000:03d}.{ext}"
        if source == "camera":
            return f"DSC_{4000 + self.counter:04d}.{ext.upper()}"
        if source == "whatsapp":
            return f"IMG-{taken.strftime('%Y%m%d')}-WA{self.counter % 10000:04d}.{ext}"
        if source == "screenshot":
            return f"Screenshot_{taken.strftime('%Y%m%d-%H%M%S')}.png"
        return f"photo_{self.counter:05d}.{ext}"

    # ---------------------------------------------------------------- library build
    def build_events(self) -> None:
        for spec in event_plan():
            camera = CAMERAS[hash(spec.key) % len(CAMERAS)]
            t = spec.start
            for i in range(spec.photos):
                day = i * spec.days // max(spec.photos, 1)
                minute_step = self.rng.randint(1, 14)
                t = t + timedelta(minutes=minute_step)
                taken = spec.start + timedelta(days=day, hours=(t - spec.start).seconds / 3600 % 11)
                roll = self.rng.random()
                if roll < 0.40:
                    kind, people = "group", self.rng.sample(spec.people, min(len(spec.people),
                                                                             self.rng.randint(2, 4)))
                elif roll < 0.60:
                    kind, people = "portrait", [self.rng.choice(spec.people)]
                else:
                    kind, people = "scene", []
                img = self.compose(spec.keywords, people, kind)
                place = spec.place if self.rng.random() < spec.gps_fraction else None
                orientation = 1
                if self.rng.random() < 0.08:  # rotated capture with EXIF orientation
                    orientation = 6
                    img = img.transpose(Image.Transpose.ROTATE_90)
                name = self.next_name(spec.source if self.rng.random() < 0.8 else "camera", taken)
                rel = f"{spec.folder}/{name}"
                self.save(img, rel, taken, place, camera, quality=self.rng.choice([85, 90, 93]),
                          orientation=orientation)
                self.records.append(Photo(rel, taken, place, people, spec.key, "phone"))

    def build_everyday(self, count: int = 160) -> None:
        """Scattered photos that should NOT form events (too few per day)."""
        for _ in range(count):
            taken = datetime(2022, 1, 1) + timedelta(days=self.rng.randint(0, 1500),
                                                     hours=self.rng.randint(7, 22),
                                                     minutes=self.rng.randint(0, 59))
            people = [self.rng.choice([p for p, _ in PEOPLE[:5]])] if self.rng.random() < 0.4 else []
            kind = "portrait" if people else "scene"
            img = self.compose(["dog", "cat", "food", "street", "kitchen"], people, kind)
            place = "ongole" if self.rng.random() < 0.5 else None
            camera = CAMERAS[self.rng.randrange(len(CAMERAS))]
            rel = f"DCIM/Camera/{self.next_name('phone', taken)}"
            self.save(img, rel, taken, place, camera)
            self.records.append(Photo(rel, taken, place, people, None, "phone"))

    def build_whatsapp_and_downloads(self, count: int = 120) -> None:
        for i in range(count):
            taken = datetime(2023, 1, 1) + timedelta(days=self.rng.randint(0, 1000))
            img = self.compose(["people", "food", "flowers", "city"], [], "scene")
            img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            if i % 3 == 0:
                rel = f"WhatsApp/Media/WhatsApp Images/{self.next_name('whatsapp', taken)}"
                src = "whatsapp"
            else:
                rel = f"Downloads/{self.next_name('other', taken)}"
                src = "download"
            self.save(img, rel, taken, None, CAMERAS[0], quality=72, exif=False)
            self.records.append(Photo(rel, taken, None, [], None, src))

    def build_screenshots(self, count: int = 60) -> None:
        """Varied UI screenshots - real ones differ in layout, palette, size and content."""
        palettes = [((245, 246, 250), (255, 255, 255), (20, 20, 20)),
                    ((18, 18, 22), (34, 34, 40), (235, 235, 240)),
                    ((236, 253, 245), (255, 255, 255), (6, 78, 59)),
                    ((253, 244, 255), (255, 255, 255), (76, 5, 82))]
        words = ["invoice", "meeting", "recipe", "booking", "ticket", "score", "weather", "balance",
                 "offer", "delivery", "reminder", "photo", "playlist", "route", "otp", "receipt"]
        for _ in range(count):
            taken = datetime(2024, 1, 1) + timedelta(days=self.rng.randint(0, 700),
                                                     hours=self.rng.randint(0, 23))
            bg, card, fg = self.rng.choice(palettes)
            size = self.rng.choice([(1080, 2340), (1170, 2532), (1440, 3200), (828, 1792)])
            img = Image.new("RGB", size, bg)
            d = ImageDraw.Draw(img)
            W, H = size
            d.rectangle([0, 0, W, 90], fill=tuple(max(0, c - 20) for c in bg))
            d.text((40, 35), f"{taken.strftime('%H:%M')}   {self.rng.randint(11, 99)}%", fill=fg)
            style = self.rng.randrange(3)
            y = 150
            while y < H - 200:
                h = self.rng.randint(120, 420)
                if style == 0:
                    left = self.rng.random() < 0.5
                    w = self.rng.randint(int(W * 0.4), int(W * 0.85))
                    x0 = 40 if left else W - 40 - w
                    d.rounded_rectangle([x0, y, x0 + w, y + h], radius=28, fill=card)
                    for line in range(max(1, h // 60)):
                        d.text((x0 + 30, y + 24 + line * 46),
                               " ".join(self.rng.choice(words) for _ in range(self.rng.randint(2, 5))), fill=fg)
                elif style == 1:
                    d.rounded_rectangle([30, y, W - 30, y + h], radius=20, fill=card)
                    d.rectangle([60, y + 20, 60 + self.rng.randint(120, 400), y + 20 + min(h - 60, 200)],
                                fill=tuple(self.rng.randrange(60, 220) for _ in range(3)))
                    d.text((60, y + h - 50), " ".join(self.rng.choice(words) for _ in range(3)), fill=fg)
                else:
                    d.rectangle([0, y, W, y + 90], fill=card)
                    d.text((60, y + 30), f"{self.rng.choice(words).title()}  -  {self.rng.randint(1, 999)}", fill=fg)
                    h = 90
                y += h + self.rng.randint(12, 40)
            rel = f"Pictures/Screenshots/{self.next_name('screenshot', taken)}"
            self.save(img, rel, taken, None, CAMERAS[0], exif=False, fmt="PNG")
            self.records.append(Photo(rel, taken, None, [], None, "screenshot"))

    def build_duplicates(self) -> None:
        """Duplicate families derived from existing camera photos."""
        base = [r for r in self.records if r.event and not r.corrupt]
        self.rng.shuffle(base)
        plan = [("exact", 40), ("resized", 40), ("compressed", 20), ("edited", 20), ("cropped", 15), ("screenshot", 10)]
        i = 0
        for kind, n in plan:
            for _ in range(n):
                if i >= len(base):
                    return
                src = base[i]
                i += 1
                group = f"dup_{kind}_{i}"
                src.dup_group, src.dup_kind = group, "original"
                src_path = self.out / src.rel_path
                taken = src.taken
                if kind == "exact":
                    rel = f"Backup 2024/Phone backup/{Path(src.rel_path).name}"
                    dest = self.out / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, dest)
                elif kind == "resized":
                    img = Image.open(src_path).convert("RGB")
                    img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                    rel = f"WhatsApp/Media/WhatsApp Images/{self.next_name('whatsapp', taken)}"
                    self.save(img, rel, taken, None, CAMERAS[0], quality=70, exif=False)
                elif kind == "compressed":
                    img = Image.open(src_path).convert("RGB")
                    rel = f"Compressed/{Path(src.rel_path).stem}_small.jpg"
                    self.save(img, rel, taken, src.place, CAMERAS[0], quality=45)
                elif kind == "edited":
                    img = Image.open(src_path).convert("RGB")
                    img = ImageEnhance.Brightness(img).enhance(1.18)
                    img = ImageEnhance.Contrast(img).enhance(1.15)
                    img = ImageEnhance.Color(img).enhance(1.2)
                    rel = f"Edited/{Path(src.rel_path).stem}-edited.jpg"
                    self.save(img, rel, taken, src.place, CAMERAS[0], quality=92, software="Snapseed 2.19")
                elif kind == "cropped":
                    img = Image.open(src_path).convert("RGB")
                    w, h = img.size
                    img = img.crop((int(w * 0.08), int(h * 0.08), int(w * 0.94), int(h * 0.94)))
                    rel = f"Edited/{Path(src.rel_path).stem}_crop.jpg"
                    self.save(img, rel, taken, src.place, CAMERAS[0], quality=88)
                else:  # screenshot of a photo
                    img = Image.open(src_path).convert("RGB")
                    img.thumbnail((980, 1600), Image.Resampling.LANCZOS)
                    canvas = Image.new("RGB", (1080, 2340), (10, 10, 12))
                    canvas.paste(img, ((1080 - img.width) // 2, (2340 - img.height) // 2))
                    d = ImageDraw.Draw(canvas)
                    d.text((40, 30), "9:41", fill=(255, 255, 255))
                    d.text((40, 2270), "◁    ○    □", fill=(180, 180, 180))
                    rel = f"Pictures/Screenshots/{self.next_name('screenshot', taken)}"
                    self.save(canvas, rel, taken, None, CAMERAS[0], exif=False, fmt="PNG")
                source = {"screenshot": "screenshot", "resized": "whatsapp",
                          "edited": "edited"}.get(kind, "phone")
                self.records.append(Photo(rel, taken,
                                          src.place if kind in ("compressed", "edited", "cropped") else None,
                                          src.people, src.event if kind != "screenshot" else None, source,
                                          dup_group=group, dup_kind=kind, origin=src.rel_path))

    def build_bursts(self, groups: int = 25) -> None:
        base = [r for r in self.records if r.event and not r.dup_group]
        self.rng.shuffle(base)
        for g in range(min(groups, len(base))):
            src = base[g]
            img0 = Image.open(self.out / src.rel_path).convert("RGB")
            gid = f"burst_{g}"
            for k in range(self.rng.randint(2, 4)):
                taken = src.taken + timedelta(seconds=1 + k)
                w, h = img0.size
                dx, dy = self.rng.randint(4, 28), self.rng.randint(4, 28)
                img = img0.crop((dx, dy, w - (30 - dx), h - (30 - dy))).resize((w, h), Image.Resampling.LANCZOS)
                img = ImageEnhance.Brightness(img).enhance(self.rng.uniform(0.97, 1.03))
                rel = f"{Path(src.rel_path).parent.as_posix()}/{self.next_name('phone', taken)}"
                self.save(img, rel, taken, src.place, CAMERAS[0], quality=90)
                self.records.append(Photo(rel, taken, src.place, src.people, src.event, "phone", burst_group=gid))

    def build_formats(self) -> None:
        """Alternative container formats the indexer must handle."""
        base = [r for r in self.records if r.event and not r.dup_group][:40]
        for i, src in enumerate(base[:30]):
            img = Image.open(self.out / src.rel_path).convert("RGB")
            img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
            taken = src.taken
            if i % 3 == 0:
                rel = f"Formats/webp/{Path(src.rel_path).stem}.webp"
                self.save(img, rel, taken, src.place, CAMERAS[0], fmt="WEBP", quality=85)
                note = "webp"
            elif i % 3 == 1:
                rel = f"Formats/png/{Path(src.rel_path).stem}.png"
                self.save(img, rel, taken, src.place, CAMERAS[0], fmt="PNG")
                note = "png"
            else:
                rel = f"Formats/tiff/{Path(src.rel_path).stem}.tiff"
                self.save(img, rel, taken, src.place, CAMERAS[0], fmt="TIFF")
                note = "tiff"
            group = src.dup_group or f"dup_format_{i}"
            src.dup_group, src.dup_kind = group, "original"
            self.records.append(Photo(rel, taken, src.place, src.people, None, "phone", note=note,
                                      dup_group=group, dup_kind="reencoded", origin=src.rel_path))
        # HEIC, if the encoder is available
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            for src in base[30:40]:
                img = Image.open(self.out / src.rel_path).convert("RGB")
                img.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
                rel = f"Formats/heic/{Path(src.rel_path).stem}.heic"
                dest = self.out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                img.save(dest, "HEIF", quality=80)
                os.utime(dest, (src.taken.timestamp(), src.taken.timestamp()))
                group = src.dup_group or f"dup_heic_{src.rel_path}"
                src.dup_group, src.dup_kind = group, "original"
                self.records.append(Photo(rel, src.taken, src.place, src.people, None, "phone", note="heic",
                                          dup_group=group, dup_kind="reencoded", origin=src.rel_path))
        except Exception as exc:  # pragma: no cover
            print("HEIC generation skipped:", exc)

    def build_edge_cases(self) -> None:
        edge = self.out / "Edge cases"
        edge.mkdir(parents=True, exist_ok=True)
        rng = self.rng
        good = [r for r in self.records if r.event][:20]
        src_path = self.out / good[0].rel_path
        now = datetime(2024, 6, 1, 12, 0)

        # truncated JPEG (still partially decodable)
        data = src_path.read_bytes()
        (edge / "truncated.jpg").write_bytes(data[: int(len(data) * 0.55)])
        self.records.append(Photo("Edge cases/truncated.jpg", good[0].taken, None, [], None, "phone",
                                  note="truncated", origin=good[0].rel_path))
        # random bytes with an image extension
        (edge / "not_an_image.jpg").write_bytes(bytes(rng.randrange(256) for _ in range(5000)))
        self.records.append(Photo("Edge cases/not_an_image.jpg", now, None, [], None, "unknown", corrupt=True,
                                  note="garbage"))
        # zero-byte file (the scanner must skip it)
        (edge / "empty.jpg").write_bytes(b"")
        self.records.append(Photo("Edge cases/empty.jpg", now, None, [], None, "unknown", corrupt=True,
                                  note="zero-byte (skipped by scanner)"))
        # JPEG bytes with a .png extension
        shutil.copy2(src_path, edge / "actually_jpeg.png")
        self.records.append(Photo("Edge cases/actually_jpeg.png", good[0].taken, None, [], None, "phone",
                                  note="wrong extension", origin=good[0].rel_path))
        # grayscale, CMYK, tiny, panorama
        img = Image.open(src_path).convert("L")
        img.save(edge / "grayscale.jpg", quality=88)
        _touch(edge / "grayscale.jpg", now)
        self.records.append(Photo("Edge cases/grayscale.jpg", now, None, [], None, "unknown", note="grayscale",
                                  origin=good[0].rel_path))
        Image.open(src_path).convert("CMYK").save(edge / "cmyk.jpg", quality=88)
        _touch(edge / "cmyk.jpg", now)
        self.records.append(Photo("Edge cases/cmyk.jpg", now, None, [], None, "unknown", note="cmyk",
                                  origin=good[0].rel_path))
        Image.open(src_path).resize((48, 36)).save(edge / "tiny.jpg", quality=80)
        _touch(edge / "tiny.jpg", now)
        self.records.append(Photo("Edge cases/tiny.jpg", now, None, [], None, "unknown", note="tiny",
                                  origin=good[0].rel_path))
        pano = Image.open(src_path).resize((9000, 1400), Image.Resampling.LANCZOS)
        pano.save(edge / "panorama.jpg", quality=70)
        _touch(edge / "panorama.jpg", now)
        self.records.append(Photo("Edge cases/panorama.jpg", now, None, [], None, "unknown", note="panorama",
                                  origin=good[0].rel_path))
        # transparency
        rgba = Image.open(src_path).convert("RGBA")
        rgba.putalpha(128)
        rgba.save(edge / "transparent.png")
        _touch(edge / "transparent.png", now)
        self.records.append(Photo("Edge cases/transparent.png", now, None, [], None, "unknown", note="alpha",
                                  origin=good[0].rel_path))
        # broken/absent EXIF variants
        img = Image.open(src_path).convert("RGB")
        img.save(edge / "no_exif.jpg", quality=85)
        _touch(edge / "no_exif.jpg", now)
        self.records.append(Photo("Edge cases/no_exif.jpg", now, None, [], None, "unknown", note="no exif",
                                  origin=good[0].rel_path))
        self.save(img, "Edge cases/zero_gps.jpg", now, None, CAMERAS[0])
        zexif = piexif.load(str(edge / "zero_gps.jpg"))
        zexif["GPS"] = {piexif.GPSIFD.GPSLatitudeRef: b"N", piexif.GPSIFD.GPSLatitude: ((0, 1), (0, 1), (0, 1)),
                        piexif.GPSIFD.GPSLongitudeRef: b"E", piexif.GPSIFD.GPSLongitude: ((0, 1), (0, 1), (0, 1))}
        piexif.insert(piexif.dump(zexif), str(edge / "zero_gps.jpg"))
        self.records.append(Photo("Edge cases/zero_gps.jpg", now, None, [], None, "unknown", note="gps 0,0",
                                  origin=good[0].rel_path))
        # implausible dates
        self.save(img, "Edge cases/future_date.jpg", datetime(2038, 1, 1, 10, 0), None, CAMERAS[0])
        self.records.append(Photo("Edge cases/future_date.jpg", datetime(2038, 1, 1, 10), None, [], None,
                                  "unknown", note="future date", origin=good[0].rel_path))
        self.save(img, "Edge cases/epoch_date.jpg", datetime(1970, 1, 2, 3, 4), None, CAMERAS[0])
        self.records.append(Photo("Edge cases/epoch_date.jpg", datetime(1970, 1, 2, 3, 4), None, [], None,
                                  "unknown", note="1970 date", origin=good[0].rel_path))
        # unicode + long names + deep nesting + uppercase extension
        self.save(img, "Edge cases/తెలుగు ఫోటో 📸.jpg", now, None, CAMERAS[0])
        self.records.append(Photo("Edge cases/తెలుగు ఫోటో 📸.jpg", now, None, [], None, "unknown", note="unicode", origin=good[0].rel_path))
        long_name = "a_very_long_filename_" + "x" * 120 + ".jpg"
        self.save(img, f"Edge cases/{long_name}", now, None, CAMERAS[0])
        self.records.append(Photo(f"Edge cases/{long_name}", now, None, [], None, "unknown", note="long name", origin=good[0].rel_path))
        deep = "Deep/" + "/".join(f"level{i}" for i in range(8))
        self.save(img, f"{deep}/nested.JPG", now, None, CAMERAS[0])
        self.records.append(Photo(f"{deep}/nested.JPG", now, None, [], None, "unknown", note="deep nesting", origin=good[0].rel_path))
        # folder that should be ignored entirely
        (self.out / "@eaDir").mkdir(exist_ok=True)
        shutil.copy2(src_path, self.out / "@eaDir" / "thumb.jpg")
        # a non-image file
        (edge / "notes.txt").write_text("not an image", encoding="utf-8")

    def write_ground_truth(self) -> None:
        for r in self.records:  # POSIX separators everywhere (Windows Path renders backslashes)
            r.rel_path = r.rel_path.replace("\\", "/")
        events = {
            spec.key: {"title": spec.title, "category": spec.category, "place": PLACES[spec.place][2],
                       "start": spec.start.isoformat(), "days": spec.days, "people": spec.people}
            for spec in event_plan()
        }
        people: dict[str, list[str]] = {}
        for r in self.records:
            for p in r.people:
                people.setdefault(p, []).append(r.rel_path)
        # Duplicate families come from content provenance: every record that derives
        # its pixels from the same original belongs to one family (bursts excluded —
        # they are separate exposures, not copies).
        dup_groups: dict[str, list[str]] = {}
        burst_groups: dict[str, list[str]] = {}
        for r in self.records:
            if r.burst_group:
                burst_groups.setdefault(r.burst_group, []).append(r.rel_path)
                continue
            if r.origin:
                root = r.origin
                seen = {r.rel_path}
                while True:  # follow the chain to the ultimate original
                    parent = next((x.origin for x in self.records if x.rel_path == root and x.origin), None)
                    if not parent or parent in seen:
                        break
                    seen.add(parent)
                    root = parent
                dup_groups.setdefault(f"dup::{root}", []).append(r.rel_path)
        for key in list(dup_groups):
            original = key.split("::", 1)[1]
            dup_groups[key].append(original)
            dup_groups[key] = sorted(set(dup_groups[key]))
            if len(dup_groups[key]) < 2:
                del dup_groups[key]
        gt = {
            "generated_at": datetime.now().isoformat(),
            "photos": {r.rel_path: {"taken": r.taken.isoformat(), "place": PLACES[r.place][2] if r.place else None,
                                    "people": r.people, "event": r.event, "source": r.source,
                                    "dup_group": r.dup_group, "dup_kind": r.dup_kind, "burst_group": r.burst_group,
                                    "origin": r.origin,
                                    "corrupt": r.corrupt, "note": r.note}
                       for r in self.records},
            "events": events,
            "people": {k: sorted(set(v)) for k, v in people.items()},
            "duplicate_groups": dup_groups,
            "burst_groups": burst_groups,
            "home": "Ongole",
        }
        (self.out / "ground_truth.json").write_text(json.dumps(gt, indent=1, ensure_ascii=False), encoding="utf-8")


def _touch(path: Path, when: datetime) -> None:
    try:
        ts = when.timestamp()
        os.utime(path, (ts, ts))
    except (OSError, OverflowError, ValueError):
        pass


def _deg_to_dms(deg: float):
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = round((m_full - m) * 60 * 10000)
    return ((d, 1), (m, 1), (s, 10000))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="D:/pi_cache/testlib")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    if args.clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    b = LibraryBuilder(out, args.seed)
    print("events...")
    b.build_events()
    print("everyday...")
    b.build_everyday()
    print("whatsapp/downloads...")
    b.build_whatsapp_and_downloads()
    print("screenshots...")
    b.build_screenshots()
    print("duplicates...")
    b.build_duplicates()
    print("bursts...")
    b.build_bursts()
    print("formats...")
    b.build_formats()
    print("edge cases...")
    b.build_edge_cases()
    b.write_ground_truth()
    total = len(b.records)
    print(f"done: {total} records under {out}")
    print("people:", {k: v for k, v in sorted(b.face_usage.items(), key=lambda x: -x[1])})


if __name__ == "__main__":
    main()
