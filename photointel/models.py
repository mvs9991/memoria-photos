"""Model acquisition and warm-up.

Weights are downloaded once into <data>/models and then used entirely offline.

  * Face detection/recognition: InsightFace "buffalo_l" (SCRFD-10G + ArcFace R50,
    WebFace600K). Released for non-commercial research use.
  * Semantic embeddings: SigLIP2 ViT-B/16 via open_clip (Apache-2.0 weights on
    the Hugging Face hub).
"""
from __future__ import annotations

import io
import logging
import time
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

BUFFALO_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
NEEDED = ("det_10g.onnx", "w600k_r50.onnx")


def face_models_present(models_dir: Path) -> bool:
    d = Path(models_dir) / "insightface" / "buffalo_l"
    return all((d / n).exists() for n in NEEDED)


def download_face_models(models_dir: Path, force: bool = False) -> dict:
    dest = Path(models_dir) / "insightface" / "buffalo_l"
    if face_models_present(models_dir) and not force:
        return {"status": "present", "path": str(dest)}
    dest.mkdir(parents=True, exist_ok=True)
    log.info("Downloading face models from %s (~280 MB)", BUFFALO_URL)
    req = urllib.request.Request(BUFFALO_URL, headers={"User-Agent": "Memoria/0.1"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = resp.read()
    written = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            base = Path(name).name
            if base in NEEDED:
                (dest / base).write_bytes(z.read(name))
                written.append(base)
    return {"status": "downloaded", "path": str(dest), "files": written}


def warm_models(ctx) -> dict:
    """Load every model once (downloads the semantic weights if needed) and report timings."""
    out: dict = {"device": ctx.device}
    t0 = time.time()
    try:
        fe = ctx.face_engine()
        out["face"] = {"loaded_s": round(time.time() - t0, 1), "det_size": fe.det_size}
    except Exception as exc:
        out["face"] = {"error": str(exc)}
    t0 = time.time()
    try:
        sm = ctx.semantic_model()
        out["semantic"] = {"loaded_s": round(time.time() - t0, 1), "name": sm.name,
                           "pretrained": sm.pretrained, "dim": sm.dim, "input": sm.size}
    except Exception as exc:
        out["semantic"] = {"error": str(exc)}
    return out
