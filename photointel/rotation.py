"""The user's rotation of a photo: kept in Memoria (photos.rotation, clockwise degrees),
applied to everything Memoria shows, never written into the file."""
from __future__ import annotations

from PIL import Image

VALID = (0, 90, 180, 270)


def normalize(degrees: int) -> int:
    return int(degrees) % 360


def rotate_image(img: Image.Image, rotation: int) -> Image.Image:
    rot = normalize(rotation or 0)
    if not rot:
        return img
    # PIL's transpose constants turn counter-clockwise; ours are clockwise.
    op = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}[rot]
    return img.transpose(op)


def rotate_box(box: list[float], rotation: int) -> list[float]:
    """A normalised (x1, y1, x2, y2) box on the file's pixels -> the same box on the turned image."""
    x1, y1, x2, y2 = box
    rot = normalize(rotation or 0)
    if rot == 90:
        return [1 - y2, x1, 1 - y1, x2]
    if rot == 180:
        return [1 - x2, 1 - y2, 1 - x1, 1 - y1]
    if rot == 270:
        return [y1, 1 - x2, y2, 1 - x1]
    return [x1, y1, x2, y2]
