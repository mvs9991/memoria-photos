"""Local face detection (SCRFD-10G) and recognition (ArcFace R50, WebFace600K).

The InsightFace "buffalo_l" ONNX weights are converted to PyTorch modules at load
time (onnx2torch) and traced with TorchScript, so a single CUDA runtime serves
every model. Converted and traced outputs were verified numerically against ONNX
Runtime (max abs diff ~1e-5).

Note: InsightFace pretrained weights are licensed for non-commercial use.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

FACE_MODEL_NAME = "scrfd10g+arcface_r50_w600k"
FACE_MODEL_VERSION = "buffalo_l-1"
FACE_EMBED_DIM = 512
BUFFALO_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"

ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


@dataclass
class DetectedFace:
    box: np.ndarray        # x1,y1,x2,y2 in work-image pixels
    kps: np.ndarray        # (5,2) in work-image pixels
    score: float


@dataclass
class FaceRecord:
    box_norm: tuple[float, float, float, float]
    kps_norm: list[float]
    det_score: float
    size_px: float
    sharpness: float
    yaw: float
    quality: float
    embedding: np.ndarray  # float32 (512,), L2-normalised
    emb_norm: float


def umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform (rotation + uniform scale + translation), 2x3."""
    n = src.shape[0]
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s_c, d_c = src - mu_s, dst - mu_d
    cov = d_c.T @ s_c / n
    U, S, Vt = np.linalg.svd(cov)
    d = np.ones(2)
    if np.linalg.det(cov) < 0:
        d[-1] = -1
    R = U @ np.diag(d) @ Vt
    var_s = (s_c ** 2).sum() / n
    scale = (S * d).sum() / var_s if var_s > 0 else 1.0
    t = mu_d - scale * R @ mu_s
    M = np.zeros((2, 3), dtype=np.float32)
    M[:, :2] = scale * R
    M[:, 2] = t
    return M


def align_face(img_rgb: np.ndarray, kps: np.ndarray, size: int = 112) -> np.ndarray:
    M = umeyama_similarity(kps.astype(np.float64), ARCFACE_DST.astype(np.float64) * (size / 112.0))
    return cv2.warpAffine(img_rgb, M, (size, size), borderValue=0.0)


def letterbox(img_rgb: np.ndarray, size: int) -> tuple[np.ndarray, float]:
    """Resize keeping aspect into a top-left aligned square canvas (SCRFD convention)."""
    h, w = img_rgb.shape[:2]
    scale = size / max(h, w)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(img_rgb, (nw, nh), interpolation=interp)
    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    canvas[:nh, :nw] = resized
    return canvas, scale


class FaceEngine:
    """Face detection + recognition.

    `prepare()` is pure CPU work and is meant to run on the parallel decode
    workers; anchor decoding, score filtering and NMS run on the GPU so only the
    surviving handful of boxes is copied back to host memory.
    """

    def __init__(self, model_dir: Path, device: str = "cuda", det_size: int = 640,
                 det_thresh: float = 0.5, nms_thresh: float = 0.4):
        import onnx
        import torch
        from onnx2torch import convert

        self.torch = torch
        self.device = device
        self.det_size = det_size
        self.det_thresh = det_thresh
        self.nms_thresh = nms_thresh
        model_dir = Path(model_dir)
        det_path = model_dir / "det_10g.onnx"
        rec_path = model_dir / "w600k_r50.onnx"
        if not det_path.exists() or not rec_path.exists():
            raise FileNotFoundError(
                f"Face models not found in {model_dir}. Run 'photointel models download' (source: {BUFFALO_URL})."
            )
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            det = convert(onnx.load(str(det_path))).eval().to(device)
            rec = convert(onnx.load(str(rec_path))).eval().to(device)
            try:
                # TorchScript removes per-node Python dispatch (~25% faster detection).
                with torch.inference_mode():
                    det = torch.jit.freeze(torch.jit.trace(
                        det, torch.zeros(1, 3, det_size, det_size, device=device), check_trace=False))
                    rec = torch.jit.freeze(torch.jit.trace(
                        rec, torch.zeros(8, 3, 112, 112, device=device), check_trace=False))
                    for _ in range(2):  # warm up freeze/optimisation passes
                        det(torch.zeros(1, 3, det_size, det_size, device=device))
                        rec(torch.zeros(8, 3, 112, 112, device=device))
            except Exception as exc:  # pragma: no cover - tracing is an optimisation only
                log.warning("TorchScript tracing failed (%s); using eager modules", exc)
        self.det = det
        self.rec = rec
        self._lock = threading.Lock()
        self._centers: dict[tuple[int, int], object] = {}
        self.fmc = 3
        self.strides = (8, 16, 32)
        self.num_anchors = 2

    # ---- preprocessing (CPU, runs on worker threads) -------------------------------------
    def prepare(self, img_rgb: np.ndarray, det_size: int | None = None) -> tuple[np.ndarray, float]:
        return letterbox(img_rgb, det_size or self.det_size)

    # ---- detection -----------------------------------------------------------------------
    def _anchor_centers(self, size: int, stride: int):
        torch = self.torch
        key = (size, stride)
        c = self._centers.get(key)
        if c is None:
            h = w = size // stride
            ys, xs = torch.meshgrid(torch.arange(h, device=self.device, dtype=torch.float32),
                                    torch.arange(w, device=self.device, dtype=torch.float32), indexing="ij")
            centers = (torch.stack([xs, ys], dim=-1).reshape(-1, 2) * stride)
            c = centers.repeat_interleave(self.num_anchors, dim=0)
            self._centers[key] = c
        return c

    def detect_prepared(self, canvas: np.ndarray, scale: float, img_hw: tuple[int, int],
                        max_faces: int = 64) -> list[DetectedFace]:
        torch = self.torch
        from torchvision.ops import nms as tv_nms

        size = canvas.shape[0]
        with self._lock, torch.inference_mode():
            t = torch.from_numpy(canvas).to(self.device, non_blocking=True)
            t = t.permute(2, 0, 1).unsqueeze(0).float().sub_(127.5).div_(128.0)
            outs = self.det(t)
            boxes_l, scores_l, kps_l = [], [], []
            for idx, stride in enumerate(self.strides):
                scores = outs[idx].reshape(-1)
                keep = torch.nonzero(scores >= self.det_thresh, as_tuple=True)[0]
                if keep.numel() == 0:
                    continue
                centers = self._anchor_centers(size, stride)[keep]
                b = outs[idx + self.fmc].reshape(-1, 4)[keep] * stride
                k = outs[idx + self.fmc * 2].reshape(-1, 10)[keep] * stride
                boxes_l.append(torch.stack([centers[:, 0] - b[:, 0], centers[:, 1] - b[:, 1],
                                            centers[:, 0] + b[:, 2], centers[:, 1] + b[:, 3]], dim=-1))
                kp = torch.empty((keep.numel(), 5, 2), device=self.device, dtype=torch.float32)
                kp[:, :, 0] = centers[:, 0:1] + k[:, 0::2]
                kp[:, :, 1] = centers[:, 1:2] + k[:, 1::2]
                kps_l.append(kp)
                scores_l.append(scores[keep])
            if not scores_l:
                return []
            boxes = torch.cat(boxes_l)
            scores = torch.cat(scores_l)
            kps = torch.cat(kps_l)
            keep = tv_nms(boxes, scores, self.nms_thresh)[:max_faces]
            boxes_np = (boxes[keep] / scale).cpu().numpy()
            kps_np = (kps[keep] / scale).cpu().numpy()
            scores_np = scores[keep].cpu().numpy()
        H, W = img_hw
        out = []
        for box, kp, sc in zip(boxes_np, kps_np, scores_np):
            x1, y1, x2, y2 = box
            b = np.array([max(0.0, float(x1)), max(0.0, float(y1)), min(float(W), float(x2)), min(float(H), float(y2))],
                         dtype=np.float32)
            if b[2] - b[0] < 4 or b[3] - b[1] < 4:
                continue
            out.append(DetectedFace(box=b, kps=kp.astype(np.float32), score=float(sc)))
        return out

    def detect(self, img_rgb: np.ndarray, max_faces: int = 64) -> list[DetectedFace]:
        canvas, scale = self.prepare(img_rgb)
        return self.detect_prepared(canvas, scale, img_rgb.shape[:2], max_faces)

    # ---- recognition ----------------------------------------------------------------------
    def embed(self, crops: list[np.ndarray], batch_size: int = 32) -> tuple[np.ndarray, np.ndarray]:
        """crops: aligned RGB uint8 112x112. Returns (normalised embeddings, raw feature norms)."""
        torch = self.torch
        if not crops:
            return np.zeros((0, FACE_EMBED_DIM), np.float32), np.zeros((0,), np.float32)
        embs, norms = [], []
        for i in range(0, len(crops), batch_size):
            batch = np.stack(crops[i:i + batch_size])
            with self._lock, torch.inference_mode():
                t = torch.from_numpy(batch).to(self.device, non_blocking=True)
                t = t.permute(0, 3, 1, 2).float().sub_(127.5).div_(127.5)
                f = self.rec(t).float()
                n = torch.linalg.norm(f, dim=1)
                emb = (f / n.clamp_min(1e-6)[:, None]).cpu().numpy()
                nrm = n.cpu().numpy()
            embs.append(emb)
            norms.append(nrm)
        return np.concatenate(embs), np.concatenate(norms)

    # ---- batched pipeline (amortises per-call GPU launch overhead) --------------------------
    def analyze_batch(self, items: list[tuple], min_size_px: float = 28, min_score: float = 0.55,
                      embed_batch: int = 64) -> list[list[FaceRecord]]:
        """items: (work_rgb, prepared|None, orig_w, orig_h). Returns face records per item.

        Detection must run per image (the traced graph is not batch-safe — verified), but every
        crop in the batch is embedded in one call: ~6 ms/face instead of ~21 ms for 1-2 faces.
        """
        per_item_dets: list[list[tuple[DetectedFace, float]]] = []
        crops: list[np.ndarray] = []
        owners: list[int] = []
        for i, (work_rgb, prepared, orig_w, orig_h) in enumerate(items):
            H, W = work_rgb.shape[:2]
            canvas, scale = prepared if prepared is not None else self.prepare(work_rgb)
            try:
                dets = self.detect_prepared(canvas, scale, (H, W))
            except Exception:
                log.exception("Face detection failed for item %d", i)
                dets = []
            px_scale = orig_w / W if W else 1.0
            kept = []
            for d in dets:
                size_px = float(min(d.box[2] - d.box[0], d.box[3] - d.box[1]) * px_scale)
                if d.score < min_score or size_px < min_size_px:
                    continue
                kept.append((d, size_px))
                crops.append(align_face(work_rgb, d.kps))
                owners.append(i)
            per_item_dets.append(kept)
        embs, norms = (self.embed(crops, batch_size=embed_batch) if crops
                       else (np.zeros((0, FACE_EMBED_DIM), np.float32), np.zeros((0,), np.float32)))
        out: list[list[FaceRecord]] = [[] for _ in items]
        for j, (crop, emb, norm) in enumerate(zip(crops, embs, norms)):
            i = owners[j]
            work_rgb, _, orig_w, orig_h = items[i]
            H, W = work_rgb.shape[:2]
            d, size_px = per_item_dets[i][len(out[i])]
            gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            sharp = float(cv2.Laplacian(gray[20:100, 16:96], cv2.CV_32F).var())
            yaw = face_yaw(d.kps)
            q = face_quality(d.score, size_px * (W / orig_w if orig_w else 1.0), sharp, yaw, float(norm))
            box = d.box
            out[i].append(FaceRecord(
                box_norm=(float(box[0] / W), float(box[1] / H), float(box[2] / W), float(box[3] / H)),
                kps_norm=[round(float(v), 5) for p in d.kps for v in (p[0] / W, p[1] / H)],
                det_score=d.score, size_px=size_px, sharpness=sharp, yaw=yaw, quality=q,
                embedding=emb.astype(np.float32), emb_norm=float(norm),
            ))
        return out

    # ---- full pipeline ---------------------------------------------------------------------
    def analyze(self, work_rgb: np.ndarray, orig_w: int, orig_h: int, min_size_px: float = 28,
                min_score: float = 0.55, prepared: tuple[np.ndarray, float] | None = None) -> list[FaceRecord]:
        H, W = work_rgb.shape[:2]
        canvas, scale = prepared if prepared is not None else self.prepare(work_rgb)
        dets = self.detect_prepared(canvas, scale, (H, W))
        if not dets:
            return []
        px_scale = orig_w / W if W else 1.0
        kept, crops = [], []
        for d in dets:
            size_px = float(min(d.box[2] - d.box[0], d.box[3] - d.box[1]) * px_scale)
            if d.score < min_score or size_px < min_size_px:
                continue
            kept.append((d, size_px))
            crops.append(align_face(work_rgb, d.kps))
        if not crops:
            return []
        embs, norms = self.embed(crops)
        records = []
        for (d, size_px), crop, emb, norm in zip(kept, crops, embs, norms):
            gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            sharp = float(cv2.Laplacian(gray[20:100, 16:96], cv2.CV_32F).var())
            yaw = face_yaw(d.kps)
            q = face_quality(d.score, size_px * (W / orig_w if orig_w else 1.0), sharp, yaw, float(norm))
            box = d.box
            records.append(FaceRecord(
                box_norm=(float(box[0] / W), float(box[1] / H), float(box[2] / W), float(box[3] / H)),
                kps_norm=[round(float(v), 5) for p in d.kps for v in (p[0] / W, p[1] / H)],
                det_score=d.score, size_px=size_px, sharpness=sharp, yaw=yaw, quality=q,
                embedding=emb.astype(np.float32), emb_norm=float(norm),
            ))
        return records


def face_yaw(kps: np.ndarray) -> float:
    """Signed horizontal nose offset relative to inter-eye distance (~0 frontal, |x|>0.5 profile)."""
    le, re_, nose = kps[0], kps[1], kps[2]
    eye_dist = float(np.linalg.norm(re_ - le))
    if eye_dist < 1e-3:
        return 1.0
    mid = (le + re_) / 2
    return float(np.clip((nose[0] - mid[0]) / eye_dist, -1.5, 1.5))


def face_quality(det_score: float, work_size_px: float, sharpness: float, yaw: float, emb_norm: float) -> float:
    """Heuristic 0..1 recognisability. ArcFace feature norm correlates with face quality."""
    s_det = min(1.0, max(0.0, (det_score - 0.5) / 0.35))
    s_size = min(1.0, max(0.0, (work_size_px - 16) / 64))
    s_sharp = min(1.0, max(0.0, (float(np.log10(max(sharpness, 1.0))) - 1.0) / 1.6))
    s_pose = max(0.0, 1.0 - abs(yaw) / 0.9)
    s_norm = min(1.0, max(0.0, (emb_norm - 8.0) / 14.0))
    return float(round(0.2 * s_det + 0.2 * s_size + 0.15 * s_sharp + 0.2 * s_pose + 0.25 * s_norm, 4))


def nms(dets: np.ndarray, thresh: float) -> list[int]:
    """CPU NMS (kept for tests and CPU-only fallback paths)."""
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        ovr = w * h / (areas[i] + areas[order[1:]] - w * h)
        order = order[np.where(ovr <= thresh)[0] + 1]
    return keep
