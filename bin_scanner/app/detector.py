"""Red-bin detection logic.

Deliberately dependency-light (Pillow only, no numpy/OpenCV) and independent
of Flask/Home Assistant so it can be unit tested and reused by the standalone
calibration tool.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Tuple

from PIL import Image


@dataclass(frozen=True)
class Roi:
    """Region of interest as fractions (0-1) of the image width/height."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class HsvThresholds:
    """Pillow's HSV mode uses 0-255 for all three channels.

    Red hue wraps around 0/255, so two ranges are supported
    (e.g. 0-12 and 245-255) to cover both ends of the wheel.
    """

    hue_min: int
    hue_max: int
    hue_min2: int
    hue_max2: int
    min_saturation: int
    min_value: int


def _is_red(h: int, s: int, v: int, t: HsvThresholds) -> bool:
    if s < t.min_saturation or v < t.min_value:
        return False
    return (t.hue_min <= h <= t.hue_max) or (t.hue_min2 <= h <= t.hue_max2)


def crop_roi(image: Image.Image, roi: Roi) -> Image.Image:
    width, height = image.size
    left = int(roi.x * width)
    top = int(roi.y * height)
    right = min(width, left + int(roi.width * width))
    bottom = min(height, top + int(roi.height * height))
    if right <= left or bottom <= top:
        raise ValueError("ROI resolves to an empty region of the image")
    return image.crop((left, top, right, bottom))


def red_pixel_percent(image: Image.Image, roi: Roi, thresholds: HsvThresholds) -> float:
    cropped = crop_roi(image.convert("RGB"), roi)
    hsv_pixels = cropped.convert("HSV").getdata()
    total = cropped.width * cropped.height
    if total == 0:
        return 0.0
    red_count = sum(1 for h, s, v in hsv_pixels if _is_red(h, s, v, thresholds))
    return 100.0 * red_count / total


def detect_red_bin(
    image_bytes: bytes,
    roi: Roi,
    thresholds: HsvThresholds,
    threshold_percent: float,
) -> Tuple[bool, float]:
    """Returns (detected, red_pixel_percent)."""
    image = Image.open(BytesIO(image_bytes))
    image.load()
    pct = red_pixel_percent(image, roi, thresholds)
    return pct >= threshold_percent, pct
