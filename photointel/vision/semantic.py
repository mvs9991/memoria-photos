"""Image/text embeddings (SigLIP2 via open_clip) for semantic search, tags and near-duplicates."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


class SemanticModel:
    def __init__(self, name: str, pretrained: str, device: str, cache_dir: Path):
        import open_clip
        import torch

        self.torch = torch
        self.name = name
        self.pretrained = pretrained
        self.device = device
        model, _, _ = open_clip.create_model_and_transforms(name, pretrained=pretrained, cache_dir=str(cache_dir))
        self.model = model.eval().to(device)
        self.tokenizer = open_clip.get_tokenizer(name, cache_dir=str(cache_dir))
        cfg = getattr(self.model.visual, "preprocess_cfg", {}) or {}
        size = cfg.get("size", 224)
        self.size = (size, size) if isinstance(size, int) else tuple(size)
        self.mean = np.array(cfg.get("mean", (0.48145466, 0.4578275, 0.40821073)), dtype=np.float32)
        self.std = np.array(cfg.get("std", (0.26862954, 0.26130258, 0.27577711)), dtype=np.float32)
        self.resize_mode = cfg.get("resize_mode", "shortest")
        self.use_amp = device == "cuda"
        with torch.inference_mode():
            probe = self.model.encode_text(self.tokenizer(["a photo"]).to(device))
        self.dim = int(probe.shape[-1])
        self.logit_scale = float(self.model.logit_scale.exp().item()) if hasattr(self.model, "logit_scale") else 100.0
        lb = getattr(self.model, "logit_bias", None)
        self.logit_bias = float(lb.item()) if lb is not None else None
        self._lock = threading.Lock()
        self._text_cache: dict[str, np.ndarray] = {}

    @property
    def version(self) -> str:
        return f"{self.pretrained}-{self.size[0]}"

    def preprocess(self, img: Image.Image) -> np.ndarray:
        """CPU-side resize to model input (uint8 HWC). Thread-safe."""
        w, h = self.size
        if self.resize_mode == "squash":
            out = img.resize((w, h), Image.Resampling.BICUBIC, reducing_gap=2.0)
        else:  # shortest side + centre crop
            iw, ih = img.size
            s = max(w / iw, h / ih)
            nw, nh = max(w, round(iw * s)), max(h, round(ih * s))
            r = img.resize((nw, nh), Image.Resampling.BICUBIC, reducing_gap=2.0)
            left, top = (nw - w) // 2, (nh - h) // 2
            out = r.crop((left, top, left + w, top + h))
        return np.asarray(out.convert("RGB"), dtype=np.uint8)

    def encode_images(self, batch_uint8: np.ndarray) -> np.ndarray:
        torch = self.torch
        x = torch.from_numpy(batch_uint8).to(self.device, non_blocking=True)
        x = x.permute(0, 3, 1, 2).float().div_(255.0)
        mean = torch.tensor(self.mean, device=self.device).view(1, 3, 1, 1)
        std = torch.tensor(self.std, device=self.device).view(1, 3, 1, 1)
        x = (x - mean) / std
        with self._lock, torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16, enabled=self.use_amp):
            f = self.model.encode_image(x)
        f = f.float()
        f = f / f.norm(dim=-1, keepdim=True).clamp_min(1e-6)
        return f.cpu().numpy()

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        torch = self.torch
        missing = [t for t in texts if t not in self._text_cache]
        for i in range(0, len(missing), 64):
            chunk = missing[i:i + 64]
            tokens = self.tokenizer(chunk).to(self.device)
            with self._lock, torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16, enabled=self.use_amp):
                f = self.model.encode_text(tokens)
            f = f.float()
            f = (f / f.norm(dim=-1, keepdim=True).clamp_min(1e-6)).cpu().numpy()
            for t, v in zip(chunk, f):
                self._text_cache[t] = v
        return np.stack([self._text_cache[t] for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def probability(self, sims: np.ndarray) -> np.ndarray:
        """Calibrated-ish match probability. SigLIP is trained with a sigmoid loss."""
        if self.logit_bias is not None:
            return 1.0 / (1.0 + np.exp(-(sims * self.logit_scale + self.logit_bias)))
        return 1.0 / (1.0 + np.exp(-(sims - 0.25) * 30))

