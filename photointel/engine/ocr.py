"""Text in photos (OCR), for searching receipts, documents, signs and screenshots.

Uses RapidOCR: PaddleOCR's detection + recognition models on ONNX Runtime, CPU, local.
OCR costs roughly a second per photo on a laptop CPU, so by default it runs only on
photos likely to contain text — screenshots, downloads and anything tagged as a
document, receipt, whiteboard, ID card, meme or book. `photointel ocr --all` reads the
rest. Each photo records which OCR model read it; an empty result is stored as '' so
it is not re-read on every run.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

import numpy as np

from .. import db, imaging

log = logging.getLogger(__name__)

OCR_MODEL_NAME = "rapidocr-ppocr"
OCR_MODEL_VERSION = "1"
MIN_LINE_CONFIDENCE = 0.6
TEXTY_TAGS = ("document", "receipt", "screenshot", "whiteboard", "id card", "meme", "book")
TEXTY_TAG_MIN_SCORE = 1.5
OCR_MAX_SIDE = 1600


class OcrEngine:
    """Lazily loaded; one recognition at a time (the ONNX sessions are shared)."""

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()
        self._lock = threading.Lock()

    def read(self, rgb: np.ndarray) -> str:
        with self._lock:
            result, _ = self._engine(rgb)
        lines = []
        for item in result or []:
            text, conf = item[1], float(item[2])
            text = " ".join(str(text).split())
            if conf >= MIN_LINE_CONFIDENCE and len(text) >= 2:
                lines.append(text)
        return "\n".join(lines)


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except Exception:
        return False


def ocr_model_id(conn: sqlite3.Connection) -> int:
    mid = db.register_model(conn, "ocr", OCR_MODEL_NAME, OCR_MODEL_VERSION, None)
    db.set_active_model(conn, "ocr", mid)
    conn.commit()
    return mid


def candidates(conn: sqlite3.Connection, model_id: int, everything: bool = False,
               limit: int | None = None) -> list:
    where = ["p.status = 'ok'", "p.media_type = 'image'", "(p.ocr_model IS NULL OR p.ocr_model != ?)"]
    args: list = [model_id]
    if not everything:
        marks = ",".join("?" * len(TEXTY_TAGS))
        where.append(
            f"""(COALESCE(p.source_kind, '') IN ('screenshot', 'download', 'scan')
                 OR EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
                            WHERE pt.photo_id = p.id AND t.name IN ({marks}) AND pt.score >= ?))""")
        args += [*TEXTY_TAGS, TEXTY_TAG_MIN_SCORE]
    sql = (f"SELECT p.id, r.path AS root, p.rel_path FROM photos p JOIN roots r ON r.id = p.root_id "
           f"WHERE {' AND '.join(where)} ORDER BY p.id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def ocr_photos(ctx, conn: sqlite3.Connection, everything: bool = False, limit: int | None = None,
               engine=None, progress=None, should_stop=None) -> dict:
    if engine is None:
        if not available():
            return {"status": "rapidocr not installed"}
        engine = OcrEngine()
    model_id = ocr_model_id(conn)
    rows = candidates(conn, model_id, everything=everything, limit=limit)
    t0 = time.time()
    done = with_text = failed = 0
    for i, r in enumerate(rows):
        if should_stop and should_stop():
            break
        try:
            img = imaging.decode(os.path.join(r["root"], r["rel_path"]), max_side=OCR_MAX_SIDE).image
            text = engine.read(np.asarray(img, dtype=np.uint8))
        except Exception as exc:
            failed += 1
            log.debug("OCR failed for photo %s: %s", r["id"], exc)
            text = ""
        conn.execute("UPDATE photos SET ocr_text = ?, ocr_model = ? WHERE id = ?", (text, model_id, r["id"]))
        done += 1
        with_text += 1 if text else 0
        if i % 20 == 19:
            conn.commit()
            if progress:
                progress(i + 1, len(rows))
    conn.commit()
    out = {"read": done, "with_text": with_text, "failed": failed, "seconds": round(time.time() - t0, 1)}
    log.info("OCR: %s", out)
    return out
