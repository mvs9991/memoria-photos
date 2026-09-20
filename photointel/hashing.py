"""Cryptographic and perceptual hashing."""
from __future__ import annotations

import hashlib

import cv2
import numpy as np
from PIL import Image


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _to_signed64(v: int) -> int:
    return v - (1 << 64) if v >= (1 << 63) else v


def to_unsigned64(v: int) -> int:
    return v + (1 << 64) if v < 0 else v


def _bits_to_int(bits: np.ndarray) -> int:
    out = 0
    for b in bits.flatten():
        out = (out << 1) | int(b)
    return out


def perceptual_hashes(img: Image.Image) -> tuple[int, int]:
    """64-bit pHash (DCT) and dHash (gradient); returned as signed int64 for SQLite."""
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(small)
    low = dct[:8, :8].flatten()
    med = np.median(low[1:])
    phash = _bits_to_int(low > med)
    d = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
    dhash = _bits_to_int(d[:, 1:] > d[:, :-1])
    return _to_signed64(phash), _to_signed64(dhash)


def hamming(a: int, b: int) -> int:
    return (to_unsigned64(a) ^ to_unsigned64(b)).bit_count()


def hamming_many(ref: int, arr: np.ndarray) -> np.ndarray:
    """Vectorised Hamming distance between one hash and an int64 array."""
    x = np.bitwise_xor(arr.astype(np.int64), np.int64(ref)).view(np.uint64)
    # popcount via byte lookup table
    table = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    bytes_ = x.view(np.uint8).reshape(-1, 8)
    return table[bytes_].sum(axis=1).astype(np.int32)
