"""The small grid thumbnail (256 px), made ahead of time instead of while someone waits.

A phone's photo grid asks for the small size. It used to be made on demand from the 512 px thumbnail: one
random read of the 512 px file plus an encode (60-130 ms), and on the 5,400 rpm disk the library lives on,
1.5-2.4 s when that file had not been read recently. On the real library 64% of photos had none yet, so
scrolling into an unvisited part of the timeline waited on dozens of these at once.

New photos get it from the indexer, from the image it already has in memory. Older ones are filled in by
`backfill`, which the server runs only while nobody is using it (it reads from the same slow disk).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

from PIL import Image

from .. import imaging

log = logging.getLogger(__name__)

SMALL_SIDE = 256
SMALL_QUALITY = 72


def small_path(thumb_root: Path, sha256: str) -> Path:
    return thumb_root / sha256[:2] / f"{sha256}_sm.webp"


def save_small(img: Image.Image, thumb_root: Path, sha256: str) -> None:
    imaging.save_thumbnail(img, small_path(thumb_root, sha256), SMALL_SIDE, quality=SMALL_QUALITY)


def make_small_from_medium(thumb_root: Path, sha256: str) -> bool:
    """Make the small thumbnail from the 512 px one. False when there is nothing to make it from."""
    src = imaging.thumb_path(thumb_root, sha256)
    if not src.exists():
        return False
    with Image.open(src) as im:
        save_small(im.convert("RGB"), thumb_root, sha256)
    return True


def missing(conn: sqlite3.Connection, thumb_root: Path, limit: int) -> list[str]:
    """Content hashes of visible photos (newest first) whose small thumbnail does not exist yet."""
    out: list[str] = []
    for (sha,) in conn.execute(
            "SELECT sha256 FROM photos WHERE status = 'ok' AND hidden = 0 AND live_component = 0 "
            "AND sha256 IS NOT NULL ORDER BY taken_ts DESC"):
        if not small_path(thumb_root, sha).exists():
            out.append(sha)
            if len(out) >= limit:
                break
    return out


def backfill(conn: sqlite3.Connection, thumb_root: Path, *, may_run: Callable[[], bool],
             batch: int = 200, pause: float = 0.02) -> int:
    """Make missing small thumbnails while `may_run()` stays true. Returns how many were made."""
    made = 0
    for sha in missing(conn, thumb_root, batch):
        if not may_run():
            break
        try:
            if make_small_from_medium(thumb_root, sha):
                made += 1
        except Exception:                       # a damaged 512 px file: the on-demand path will cope
            log.debug("small thumbnail for %s failed", sha, exc_info=True)
        time.sleep(pause)                       # leave the disk to whoever comes back
    return made
