"""Process-wide application context: paths, settings, database, lazily-loaded models."""
from __future__ import annotations

import logging
import logging.handlers
import sqlite3
import threading
from pathlib import Path

from . import db
from .config import Paths, Settings, configure_model_caches, resolve_data_dir

log = logging.getLogger(__name__)


def setup_logging(paths: Paths, name: str = "photointel", level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_photointel", False) for h in root.handlers):
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(paths.logs / f"{name}.log", maxBytes=10 << 20, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh._photointel = True  # type: ignore[attr-defined]
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh._photointel = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("PIL", "urllib3", "httpx", "huggingface_hub", "timm", "open_clip", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class AppContext:
    def __init__(self, data_dir: str | Path | None = None):
        self.paths = Paths(resolve_data_dir(data_dir)).ensure()
        configure_model_caches(self.paths)
        self.settings = Settings.load(self.paths.data)
        conn = db.init_db(self.paths.db)
        conn.close()
        self._lock = threading.Lock()
        self._face_engine = None
        self._semantic = None
        self._device: str | None = None

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.paths.db)

    def reload_settings(self) -> Settings:
        self.settings = Settings.load(self.paths.data)
        return self.settings

    @property
    def device(self) -> str:
        if self._device is None:
            from .vision.device import configure_torch_threads, pick_device

            self._device = pick_device(self.settings.device)
            configure_torch_threads(self._device)
            log.info("Compute device: %s", self._device)
        return self._device

    # ---- models ---------------------------------------------------------------------------
    @property
    def face_model_dir(self) -> Path:
        return self.paths.models / "insightface" / "buffalo_l"

    def face_engine(self):
        with self._lock:
            if self._face_engine is None:
                from .vision.faces import FaceEngine

                self._face_engine = FaceEngine(self.face_model_dir, device=self.device,
                                               det_size=self.settings.face_det_size)
            return self._face_engine

    def semantic_model(self):
        with self._lock:
            if self._semantic is None:
                from .vision.semantic import SemanticModel

                self._semantic = SemanticModel(self.settings.semantic_model, self.settings.semantic_pretrained,
                                               device=self.device, cache_dir=self.paths.models / "open_clip")
            return self._semantic

    def semantic_loaded(self) -> bool:
        return self._semantic is not None

    def face_model_id(self, conn: sqlite3.Connection) -> int:
        from .vision.faces import FACE_EMBED_DIM, FACE_MODEL_NAME, FACE_MODEL_VERSION

        mid = db.register_model(conn, "face", FACE_MODEL_NAME, FACE_MODEL_VERSION, FACE_EMBED_DIM,
                                {"det_size": self.settings.face_det_size})
        db.set_active_model(conn, "face", mid)
        conn.commit()
        return mid

    def semantic_model_id(self, conn: sqlite3.Connection) -> int:
        """Model identity is (architecture, pretrained tag); the dimension is filled in once loaded."""
        mid = db.register_model(conn, "semantic", self.settings.semantic_model, self.settings.semantic_pretrained, None)
        if self._semantic is not None:
            conn.execute("UPDATE models SET dim = ? WHERE id = ? AND dim IS NULL", (self._semantic.dim, mid))
        db.set_active_model(conn, "semantic", mid)
        conn.commit()
        return mid
