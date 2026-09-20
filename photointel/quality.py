"""Deterministic photo quality signals."""
from __future__ import annotations

import math

import cv2
import numpy as np
from PIL import Image


def image_quality(img: Image.Image) -> dict:
    """Compute blur/exposure metrics on a normalised 512px grayscale version."""
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    h, w = gray.shape
    scale = 512 / max(h, w)
    if scale < 1:
        gray = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    # Use the sharpest region (photos often have intentional bokeh): 3x3 grid, take 90th pct of cell variances.
    gh, gw = gray.shape
    cells = []
    for i in range(3):
        for j in range(3):
            cell = lap[i * gh // 3:(i + 1) * gh // 3, j * gw // 3:(j + 1) * gw // 3]
            if cell.size:
                cells.append(float(cell.var()))
    blur_global = float(lap.var())
    blur = max(blur_global, float(np.percentile(cells, 90))) if cells else blur_global
    brightness = float(gray.mean()) / 255.0
    contrast = float(gray.std()) / 255.0
    clipped = float(((gray <= 3) | (gray >= 252)).mean())
    return {"blur": blur, "brightness": brightness, "contrast": contrast, "clipped": clipped}


def sharpness_score(blur: float | None) -> float:
    """Map Laplacian variance to 0..1 (log scale; ~15 very blurry, ~500+ crisp)."""
    if blur is None or blur <= 0:
        return 0.0
    return float(min(1.0, max(0.0, (math.log10(blur) - 1.2) / (2.9 - 1.2))))


def exposure_score(brightness: float | None, clipped: float | None, contrast: float | None) -> float:
    if brightness is None:
        return 0.5
    b = 1.0 - min(1.0, abs(brightness - 0.47) / 0.47) ** 1.5
    c = min(1.0, (contrast or 0) / 0.22)
    k = 1.0 - min(1.0, (clipped or 0) / 0.35)
    return float(max(0.0, 0.5 * b + 0.25 * c + 0.25 * k))


def resolution_score(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.0
    mp = width * height / 1e6
    return float(min(1.0, max(0.0, math.log2(max(mp, 0.05) / 0.3) / math.log2(12 / 0.3))))


def combined_quality(row: dict, face_quality: float | None = None, aesthetic: float | None = None) -> float:
    """0..100 overall score used for "best photos". Weights are heuristic, documented in README."""
    s = sharpness_score(row.get("blur"))
    e = exposure_score(row.get("brightness"), row.get("clipped"), row.get("contrast"))
    r = resolution_score(row.get("width"), row.get("height"))
    parts = [(s, 0.35), (e, 0.2), (r, 0.1)]
    if aesthetic is not None:
        parts.append((aesthetic, 0.25))
    if face_quality is not None:
        parts.append((face_quality, 0.10))
    total_w = sum(w for _, w in parts)
    score = sum(v * w for v, w in parts) / total_w
    kind = row.get("source_kind")
    if kind in ("screenshot",):
        score *= 0.5
    elif kind in ("whatsapp", "download"):
        score *= 0.85
    return round(100.0 * score, 2)
