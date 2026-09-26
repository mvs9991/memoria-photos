"""Dominant colours, for searches like "blue photos" or "red and white".

Read from the cached grid thumbnail (no model, no original read): each pixel of a 48-px
copy is named by hue, saturation and brightness, and a colour counts as dominant when it
covers enough of the picture — chromatic colours from 12 %, black / white / grey from
30 % (they are everywhere, so they must really dominate to mean something). Up to three
are kept, most prominent first, in photos.colors ('' when none qualifies).

The thresholds are judgement, not measured: a photo "is red" here when red covers a
good share of it, which is what a colour-only search means; "a red car" in a grey street
is left to the visual search, which reads the whole phrase.
"""
from __future__ import annotations

import logging
import sqlite3
import time

import numpy as np
from PIL import Image

from .. import imaging

log = logging.getLogger(__name__)

CHROMATIC = ("red", "orange", "yellow", "green", "blue", "purple", "pink", "brown")
NEUTRAL = ("black", "white", "grey")
NAMES = CHROMATIC + NEUTRAL
SYNONYMS = {"gray": "grey", "violet": "purple", "golden": "yellow", "gold": "yellow", "navy": "blue",
            "cyan": "blue", "teal": "green", "turquoise": "blue", "beige": "brown", "tan": "brown",
            "silver": "grey", "maroon": "red", "crimson": "red", "magenta": "pink", "lime": "green"}
MIN_SHARE = {**{c: 0.12 for c in CHROMATIC}, **{c: 0.30 for c in NEUTRAL}}


def classify(rgb: np.ndarray) -> np.ndarray:
    """(N, 3) uint8 RGB -> (N,) colour index into NAMES."""
    arr = rgb.astype(np.float32) / 255.0
    mx, mn = arr.max(1), arr.min(1)
    v = mx
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    r, g, b = arr[:, 0], arr[:, 1], arr[:, 2]
    d = np.maximum(mx - mn, 1e-6)
    h = np.select([mx == r, mx == g], [((g - b) / d) % 6, (b - r) / d + 2], (r - g) / d + 4) * 60
    out = np.empty(len(arr), np.int8)
    hue = np.select(
        [(h < 15) | (h >= 345), h < 40, h < 70, h < 165, h < 255, h < 290],
        [NAMES.index("red"), NAMES.index("orange"), NAMES.index("yellow"), NAMES.index("green"),
         NAMES.index("blue"), NAMES.index("purple")], NAMES.index("pink"))
    brown = (h >= 10) & (h < 45) & (v < 0.6) & (s > 0.25)
    out[:] = hue
    out[brown] = NAMES.index("brown")
    out[s < 0.18] = np.where(v[s < 0.18] > 0.82, NAMES.index("white"), NAMES.index("grey"))
    out[v < 0.18] = NAMES.index("black")
    return out


def dominant(img: Image.Image, k: int = 3) -> list[str]:
    small = img.convert("RGB").resize((48, 48), Image.Resampling.BILINEAR)
    idx = classify(np.asarray(small).reshape(-1, 3))
    counts = np.bincount(idx, minlength=len(NAMES)) / len(idx)
    ranked = sorted(((counts[i], NAMES[i]) for i in range(len(NAMES))), reverse=True)
    return [name for share, name in ranked if share >= MIN_SHARE[name]][:k]


def compute_colors(ctx, conn: sqlite3.Connection, limit: int | None = None) -> dict:
    t0 = time.time()
    rows = conn.execute("SELECT id, sha256 FROM photos WHERE status = 'ok' AND colors IS NULL AND sha256 IS NOT NULL"
                        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    done = missing = 0
    batch: list[tuple[str, int]] = []
    for r in rows:
        path = imaging.thumb_path(ctx.paths.thumbs, r["sha256"])
        try:
            with Image.open(path) as im:
                batch.append((",".join(dominant(im)), int(r["id"])))
                done += 1
        except (OSError, ValueError):
            missing += 1            # no thumbnail yet; tried again next time
        if len(batch) >= 500:
            conn.executemany("UPDATE photos SET colors = ? WHERE id = ?", batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany("UPDATE photos SET colors = ? WHERE id = ?", batch)
        conn.commit()
    out = {"colored": done, "no_thumbnail": missing, "seconds": round(time.time() - t0, 2)}
    log.info("Colours: %s", out)
    return out


def normalise(word: str) -> str | None:
    w = word.lower().rstrip("s") if word.lower() not in NAMES else word.lower()
    w = SYNONYMS.get(word.lower(), SYNONYMS.get(w, w))
    return w if w in NAMES else None
