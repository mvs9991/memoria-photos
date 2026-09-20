"""Runtime configuration.

Everything the system writes (database, thumbnails, model weights, caches) lives
under a single *data directory*. Original photos are only ever opened read-only.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".heic", ".heif", ".avif",
    ".tif", ".tiff", ".bmp", ".gif",
}
RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".srw", ".pef", ".nrw",
}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | RAW_EXTENSIONS

# Folders that are never photo content (OS / NAS / app metadata).
SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "@eadir", ".thumbnails", ".thumbs",
    ".trash", ".trashes", ".git", "node_modules", "__pycache__", ".picasaoriginals",
    ".spotlight-v100", ".fseventsd", "#recycle", ".tmp.drivedownload",
}


@dataclass
class Settings:
    """User-tunable settings persisted to ``<data>/settings.json``."""

    device: str = "auto"                  # auto | cuda | cpu
    workers: int = 0                      # CPU decode threads; 0 = auto
    face_det_size: int = 640              # SCRFD input size
    face_min_size_px: int = 28            # ignore smaller faces (in original pixels)
    face_min_det_score: float = 0.55
    semantic_model: str = "ViT-B-16-SigLIP2"
    semantic_pretrained: str = "webli"
    thumb_size: int = 512                 # long side of grid thumbnails
    preview_size: int = 2048              # long side of viewer previews for non-browser formats
    cluster_min_faces: int = 3            # min faces for an auto-discovered person
    event_min_photos: int = 4
    home_radius_km: float = 60.0
    # Privacy: all external services are opt-in.
    allow_online_map_tiles: bool = False
    llm_enabled: bool = False
    llm_send_images: bool = False         # never send pixels unless explicitly enabled
    llm_model: str = "claude-opus-5"
    anthropic_api_key: str = ""
    me_person_id: int | None = None       # "photos of me"

    @classmethod
    def load(cls, data_dir: Path) -> "Settings":
        path = data_dir / "settings.json"
        s = cls()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
            except Exception:  # corrupt settings must not brick the app
                pass
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_key and not s.anthropic_api_key:
            s.anthropic_api_key = env_key
        # Escape hatch for a one-off run (debugging a GPU problem, or leaving the
        # card free for another job) without editing the saved settings.
        env_device = (os.environ.get("PHOTOINTEL_DEVICE") or "").strip().lower()
        if env_device in ("auto", "cuda", "cpu"):
            s.device = env_device
        return s

    def save(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "settings.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def public_dict(self) -> dict:
        d = asdict(self)
        d["anthropic_api_key"] = bool(self.anthropic_api_key)  # never echo secrets
        return d


@dataclass
class Paths:
    data: Path
    db: Path = field(init=False)
    thumbs: Path = field(init=False)
    previews: Path = field(init=False)
    faces: Path = field(init=False)
    models: Path = field(init=False)
    geo: Path = field(init=False)
    logs: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data = Path(self.data).resolve()
        self.db = self.data / "library.db"
        self.thumbs = self.data / "cache" / "thumbs"
        self.previews = self.data / "cache" / "previews"
        self.faces = self.data / "cache" / "faces"
        # Model weights and geo data describe the machine, not the library: a
        # second library should share them rather than re-download ~300 MB each.
        self.models = Path(os.environ.get("PHOTOINTEL_MODELS") or (self.data / "models")).resolve()
        self.geo = Path(os.environ.get("PHOTOINTEL_GEO") or (self.data / "geo")).resolve()
        self.logs = self.data / "logs"

    def ensure(self) -> "Paths":
        for p in (self.data, self.thumbs, self.previews, self.faces, self.models, self.geo, self.logs):
            p.mkdir(parents=True, exist_ok=True)
        return self


def resolve_data_dir(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("PHOTOINTEL_DATA")
    return Path(env).resolve() if env else DEFAULT_DATA_DIR


def configure_model_caches(paths: Paths) -> None:
    """Keep large downloads inside the data dir (not the small system drive)."""
    os.environ.setdefault("HF_HOME", str(paths.models / "hf"))
    os.environ.setdefault("TORCH_HOME", str(paths.models / "torch"))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
