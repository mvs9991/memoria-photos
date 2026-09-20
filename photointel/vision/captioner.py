"""Local image captioning (Florence-2) for representative photos.

Captions are expensive relative to embeddings (~0.4 s/photo on a laptop GPU), so
they are *not* produced for every photo during indexing. They are generated for
the photos that get read as text — event covers, highlights, and any photo the
user opens and asks about — and cached in the database with the model id that
produced them.

Nothing leaves the machine: Florence-2 runs locally, like everything else here.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

CAPTION_MODEL_NAME = "florence2-base"
CAPTION_MODEL_REPO = "florence-community/Florence-2-base"
CAPTION_MODEL_VERSION = "1"

# Florence-2 task prompts.
TASK_CAPTION = "<CAPTION>"
TASK_DETAILED = "<DETAILED_CAPTION>"
TASK_TAGS = "<OD>"


class Captioner:
    """Lazily-loaded local captioner. Thread-safe; one generation at a time."""

    def __init__(self, device: str = "cuda", cache_dir: Path | None = None,
                 repo: str = CAPTION_MODEL_REPO):
        self.device = device
        self.repo = repo
        self.cache_dir = str(cache_dir) if cache_dir else None
        self._lock = threading.Lock()
        self._model = None
        self._processor = None
        self._dtype = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        t0 = time.time()
        self._dtype = torch.float16 if self.device == "cuda" else torch.float32
        self._processor = AutoProcessor.from_pretrained(self.repo, cache_dir=self.cache_dir)
        self._model = AutoModelForImageTextToText.from_pretrained(
            self.repo, dtype=self._dtype, cache_dir=self.cache_dir).to(self.device).eval()
        log.info("Loaded captioner %s in %.1fs", self.repo, time.time() - t0)

    def caption(self, image: Image.Image, detailed: bool = False, max_new_tokens: int = 96) -> str:
        import torch

        with self._lock:
            self._ensure_loaded()
            task = TASK_DETAILED if detailed else TASK_CAPTION
            img = image.convert("RGB")
            if max(img.size) > 1024:
                img.thumbnail((1024, 1024), Image.Resampling.BILINEAR)
            inputs = self._processor(text=task, images=img, return_tensors="pt").to(self.device, self._dtype)
            with torch.inference_mode():
                ids = self._model.generate(
                    input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                    max_new_tokens=max_new_tokens, num_beams=1, do_sample=False)
            text = self._processor.batch_decode(ids, skip_special_tokens=False)[0]
            parsed = self._processor.post_process_generation(
                text, task=task, image_size=(img.width, img.height))
            out = parsed.get(task, "")
            if isinstance(out, dict):
                out = out.get("labels", [""])[0] if out.get("labels") else ""
            return str(out).strip()

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._processor = None
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass


def caption_photos(ctx, conn, photo_ids: list[int] | None = None, limit: int = 200,
                   detailed: bool = False, progress=None) -> dict:
    """Caption photos that don't have one yet (covers and highlights first)."""
    from .. import db, imaging

    model_id = db.register_model(conn, "caption", CAPTION_MODEL_NAME, CAPTION_MODEL_VERSION, None,
                                 {"repo": CAPTION_MODEL_REPO})
    db.set_active_model(conn, "caption", model_id)
    conn.commit()

    if photo_ids:
        marks = ",".join("?" * len(photo_ids))
        rows = conn.execute(
            f"""SELECT p.id, r.path root, p.rel_path FROM photos p JOIN roots r ON r.id = p.root_id
                WHERE p.id IN ({marks}) AND p.status='ok'""", photo_ids).fetchall()
    else:
        # Priority: event covers and highlights — the photos the UI actually shows big.
        rows = conn.execute(
            """SELECT p.id, r.path root, p.rel_path FROM photos p JOIN roots r ON r.id = p.root_id
               WHERE p.status='ok' AND (p.caption IS NULL OR p.caption_model != ?)
                 AND COALESCE(p.source_kind,'') != 'screenshot'
               ORDER BY (p.id IN (SELECT cover_photo_id FROM events WHERE cover_photo_id IS NOT NULL)) DESC,
                        COALESCE(p.quality_score, 0) DESC
               LIMIT ?""", (model_id, limit)).fetchall()
    if not rows:
        return {"captioned": 0}

    captioner = Captioner(device=ctx.device, cache_dir=ctx.paths.models / "hf")
    done = failed = 0
    t0 = time.time()
    for i, r in enumerate(rows):
        path = Path(r["root"]) / r["rel_path"]
        try:
            img = imaging.decode(path, max_side=1024).image
            text = captioner.caption(img, detailed=detailed)
            if text:
                conn.execute("UPDATE photos SET caption=?, caption_model=? WHERE id=?",
                             (text, model_id, r["id"]))
                done += 1
        except Exception as exc:
            failed += 1
            log.debug("Caption failed for %s: %s", path, exc)
        if i % 20 == 19:
            conn.commit()
            if progress:
                progress(i + 1, len(rows))
    conn.commit()
    captioner.unload()
    out = {"captioned": done, "failed": failed, "seconds": round(time.time() - t0, 1)}
    log.info("Captioning: %s", out)
    return out
