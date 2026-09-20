"""Shared fixtures.

Tests never need a GPU or model weights: the face and semantic engines are
replaced by deterministic fakes, so everything except the neural nets themselves
is exercised (scanning, decoding, metadata, DB, clustering, events, duplicates,
search, API).
"""
from __future__ import annotations

import io
import sys
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import piexif
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photointel.context import AppContext  # noqa: E402
from photointel.vision.faces import FaceRecord  # noqa: E402


# ----------------------------------------------------------------- image helpers

def make_image(path: Path, size=(800, 600), colour=(120, 140, 160), taken: datetime | None = None,
               gps: tuple[float, float] | None = None, camera=("samsung", "SM-S918B"),
               orientation: int = 1, fmt: str = "JPEG", noise: int = 0, quality: int = 90) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((size[1], size[0], 3), colour, dtype=np.uint8)
    if noise:
        # Seed from the file name only. hash() is salted per process and the full
        # path contains pytest's per-run tmp counter, so either would regenerate
        # different noise on every run and make failures unreproducible.
        rng = np.random.default_rng(zlib.crc32(path.name.encode("utf-8")))
        arr = np.clip(arr.astype(np.int16) + rng.integers(-noise, noise, arr.shape), 0, 255).astype(np.uint8)
    # a few shapes so perceptual hashes differ between images
    arr[size[1] // 4: size[1] // 2, size[0] // 4: size[0] // 2] = (colour[2], colour[0], colour[1])
    img = Image.fromarray(arr)
    kwargs = {}
    if fmt == "JPEG":
        kwargs["quality"] = quality
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        if camera:
            exif_dict["0th"][piexif.ImageIFD.Make] = camera[0].encode()
            exif_dict["0th"][piexif.ImageIFD.Model] = camera[1].encode()
        exif_dict["0th"][piexif.ImageIFD.Orientation] = orientation
        if taken:
            stamp = taken.strftime("%Y:%m:%d %H:%M:%S").encode()
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = stamp
            exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = stamp
        if gps:
            lat, lon = gps
            def dms(v):
                d = int(abs(v)); m = int((abs(v) - d) * 60); s = round((((abs(v) - d) * 60) - m) * 60 * 100)
                return ((d, 1), (m, 1), (s, 100))
            exif_dict["GPS"] = {
                piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
                piexif.GPSIFD.GPSLatitude: dms(lat),
                piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
                piexif.GPSIFD.GPSLongitude: dms(lon),
            }
        if any(exif_dict[k] for k in ("0th", "Exif", "GPS")):
            kwargs["exif"] = piexif.dump(exif_dict)
    img.save(path, fmt, **kwargs)
    if taken:
        try:
            ts = taken.timestamp()
            import os

            os.utime(path, (ts, ts))
        except (OSError, OverflowError, ValueError):
            pass
    return path


# ----------------------------------------------------------------- fake models

class FakeFaceEngine:
    """Deterministic 'faces': each image colour maps to one identity vector."""

    det_size = 640
    dim = 32

    def __init__(self, faces_per_image=None):
        self.faces_per_image = faces_per_image or {}
        self.calls = 0

    def prepare(self, img_rgb, det_size=None):
        return np.zeros((8, 8, 3), np.uint8), 1.0

    def _vec(self, identity: int) -> np.ndarray:
        rng = np.random.default_rng(1000 + identity)
        v = rng.normal(size=self.dim).astype(np.float32)
        return v / np.linalg.norm(v)

    def analyze(self, work_rgb, orig_w, orig_h, min_size_px=28, min_score=0.55, prepared=None):
        self.calls += 1
        # identity = dominant red channel bucket, so tests control who appears
        identity = int(work_rgb[:, :, 0].mean()) // 40
        n = self.faces_per_image.get(identity, 1)
        out = []
        for i in range(n):
            base = self._vec(identity + i)
            noise = np.random.default_rng(self.calls * 7 + i).normal(scale=0.06, size=self.dim).astype(np.float32)
            emb = base + noise
            emb /= np.linalg.norm(emb)
            out.append(FaceRecord(
                box_norm=(0.1 + 0.2 * i, 0.1, 0.3 + 0.2 * i, 0.4), kps_norm=[0.0] * 10,
                det_score=0.9, size_px=120.0, sharpness=200.0, yaw=0.0, quality=0.8,
                embedding=emb, emb_norm=20.0))
        return out

    def analyze_batch(self, items, min_size_px=28, min_score=0.55):
        return [self.analyze(w, ow, oh, min_size_px, min_score, p) for (w, p, ow, oh) in items]


class FakeSemanticModel:
    name = "fake-clip"
    pretrained = "test"
    dim = 16
    size = (32, 32)
    resize_mode = "squash"
    logit_scale = 100.0
    logit_bias = -10.0
    version = "test-32"

    def preprocess(self, img):
        return np.asarray(img.resize((32, 32)).convert("RGB"), dtype=np.uint8)

    def encode_images(self, batch):
        out = []
        for im in batch:
            mean = im.reshape(-1, 3).mean(axis=0) / 255.0
            v = np.zeros(self.dim, dtype=np.float32)
            v[:3] = mean
            v[3] = float(im.std()) / 128.0
            v[4:] = np.sin(np.arange(self.dim - 4) * (mean.sum() + 0.1))
            out.append(v / (np.linalg.norm(v) + 1e-8))
        return np.stack(out)

    def encode_texts(self, texts):
        out = []
        for t in texts:
            rng = np.random.default_rng(zlib.crc32(t.encode("utf-8")))  # stable across processes
            v = rng.normal(size=self.dim).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)

    def probability(self, sims):
        return 1.0 / (1.0 + np.exp(-(sims * 4 - 1)))


@pytest.fixture
def ctx(tmp_path, monkeypatch) -> AppContext:
    data = tmp_path / "data"
    ctx = AppContext(data)
    ctx._device = "cpu"
    fake_face = FakeFaceEngine()
    fake_sem = FakeSemanticModel()
    monkeypatch.setattr(ctx, "face_engine", lambda: fake_face)
    monkeypatch.setattr(ctx, "semantic_model", lambda: fake_sem)
    ctx.test_face_engine = fake_face          # type: ignore[attr-defined]
    ctx.test_semantic_model = fake_sem        # type: ignore[attr-defined]
    return ctx


@pytest.fixture
def library(tmp_path) -> Path:
    """A small library: two events, two identities, a duplicate and a screenshot."""
    root = tmp_path / "lib"
    day1 = datetime(2024, 3, 9, 11, 0)
    day2 = datetime(2024, 7, 20, 9, 0)
    for i in range(6):
        make_image(root / "DCIM/Camera" / f"IMG_2024030{i}_1100{i:02d}.jpg", colour=(40, 90, 160),
                   taken=day1 + timedelta(minutes=7 * i), gps=(17.385, 78.4867))
    for i in range(5):
        make_image(root / "Trips/Goa" / f"IMG_x{i}.jpg", colour=(200, 120, 60),
                   taken=day2 + timedelta(minutes=11 * i), gps=(15.5439, 73.7553))
    # exact duplicate of the first photo
    src = root / "DCIM/Camera/IMG_20240300_110000.jpg"
    dup = root / "Backup/IMG_20240300_110000.jpg"
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_bytes(src.read_bytes())
    # a screenshot (PNG, no EXIF)
    make_image(root / "Pictures/Screenshots/Screenshot_20240610-101010.png", size=(1080, 2340),
               colour=(240, 240, 245), fmt="PNG", camera=None)
    # an unreadable file
    (root / "DCIM/Camera/broken.jpg").write_bytes(b"not an image at all")
    return root
