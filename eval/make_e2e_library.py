"""Build a small disposable library for end-to-end browser tests.

Never point this at a real library. It writes only under the folder you give it (default
D:/pi_cache/e2e/lib) and gives a mix a browser test needs: several days and folders, GPS, an exact
duplicate pair, a screenshot, a WhatsApp-style photo, a panorama and a tall photo.

    python eval/make_e2e_library.py [--out D:/pi_cache/e2e/lib]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.conftest import make_image  # noqa: E402


def build(out: Path) -> dict:
    if out.exists():
        shutil.rmtree(out)          # only ever the scratch folder this script was told to use
    out.mkdir(parents=True)
    made = {"photos": 0}

    def img(rel, **kw):
        make_image(out / rel, **kw)
        made["photos"] += 1

    day1 = datetime(2024, 3, 9, 11, 0)
    for i in range(8):
        img(f"DCIM/Camera/IMG_2024030{i}_1100{i:02d}.jpg", colour=(30 + 20 * i, 90, 160), noise=20,
            taken=day1 + timedelta(minutes=7 * i), gps=(17.385, 78.4867))
    day2 = datetime(2024, 7, 20, 9, 0)
    for i in range(6):
        img(f"Trips/Goa/IMG_x{i}.jpg", colour=(200, 60 + 25 * i, 60), noise=20,
            taken=day2 + timedelta(minutes=11 * i), gps=(15.5439, 73.7553))
    day3 = datetime(2023, 12, 25, 18, 0)
    for i in range(5):
        img(f"Family/Christmas/XMAS_{i}.jpg", colour=(40, 160, 60 + 30 * i), noise=20,
            taken=day3 + timedelta(minutes=3 * i))
    # an exact duplicate of the first camera photo
    src = out / "DCIM/Camera/IMG_20240300_110000.jpg"
    dup = out / "Backup/IMG_20240300_110000.jpg"
    dup.parent.mkdir(parents=True)
    dup.write_bytes(src.read_bytes())
    made["photos"] += 1
    img("Pictures/Screenshots/Screenshot_20240610-101010.png", size=(1080, 2340), colour=(240, 240, 245),
        fmt="PNG", camera=None)
    img("WhatsApp/IMG-20240501-WA0001.jpg", colour=(120, 120, 30), noise=20, camera=None,
        taken=datetime(2024, 5, 1, 12, 0))
    img("Misc/panorama.jpg", size=(6000, 400), colour=(10, 120, 200), noise=10, taken=datetime(2024, 8, 2, 8, 0))
    img("Misc/tall.jpg", size=(400, 3000), colour=(200, 120, 10), noise=10, taken=datetime(2024, 8, 2, 8, 5))
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="D:/pi_cache/e2e/lib")
    args = ap.parse_args()
    print(build(Path(args.out)))
