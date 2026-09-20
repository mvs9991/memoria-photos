"""Compute device selection."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Fragmentation is what kills a small card: the allocator reserves blocks it
# cannot reuse for a differently-shaped tensor. Expandable segments let it grow
# one region instead, which keeps a 4 GB card usable.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")


def pick_device(preference: str = "auto") -> str:
    import torch

    if preference == "cpu":
        return "cpu"
    if torch.cuda.is_available():
        try:
            torch.zeros(1, device="cuda")
            return "cuda"
        except Exception as exc:  # driver present but unusable (unsupported arch, OOM, ...)
            log.warning("CUDA unavailable (%s); falling back to CPU", exc)
    if preference == "cuda":
        log.warning("CUDA requested but not available; using CPU")
    return "cpu"


def gpu_memory_gb() -> float:
    """Total memory of the active GPU in GiB (0 when running on CPU)."""
    try:
        import torch

        if not torch.cuda.is_available():
            return 0.0
        return torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    except Exception:
        return 0.0


def gpu_batch_sizes(device: str) -> dict:
    """Batch sizes scaled to the card. Small cards thrash long before they OOM."""
    if device != "cuda":
        return {"pipeline": 8, "semantic": 8, "faces": 16}
    total = gpu_memory_gb()
    if total < 5:
        return {"pipeline": 12, "semantic": 12, "faces": 32}
    if total < 10:
        return {"pipeline": 24, "semantic": 24, "faces": 64}
    return {"pipeline": 48, "semantic": 48, "faces": 128}


def configure_torch_threads(device: str) -> None:
    import torch

    n = os.cpu_count() or 4
    torch.set_num_threads(max(1, n // 2) if device == "cpu" else 2)
