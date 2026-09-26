"""Bin detection logic.

Deliberately dependency-light (Pillow + stdlib only, no numpy/OpenCV/scipy)
and independent of Flask/Home Assistant so it can be unit tested and reused
by the standalone calibration tool.

Two detection modes share the same blob-analysis machinery:

- Color mode (daylight): find the largest red-hued blob in the ROI.
- Infrared mode (night, camera on IR illuminators): color is physically
  unavailable in a monochrome IR image, so instead this compares the ROI's
  luminance against a baseline built from several reference snapshots taken
  with the bin confirmed absent, and finds the largest blob of pixels that
  changed relative to that baseline.

The infrared baseline is per-pixel mean + stddev across multiple reference
samples (not a single fixed image), so a spot that's normally noisy from
night to night - streetlight cycling, moonlight, IR-illuminator gain drift -
gets a proportionally wider tolerance than a stable one, instead of every
pixel sharing one fixed threshold.

Both modes end up with a "mask of interesting pixels" that gets analyzed
identically (largest connected blob size % and bounding-box aspect ratio),
so the same noise/shape rejection applies to night detection too.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from io import BytesIO
from typing import List, Optional, Tuple

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


@dataclass(frozen=True)
class BlobAnalysis:
    """Result of scanning a mask (red pixels, or changed-from-reference pixels)
    for its largest connected blob."""

    total_pct: float  # % of the region where the mask is True (scattered + blob)
    blob_pct: float  # % of the region covered by the single largest connected blob
    blob_pixel_count: int
    aspect_ratio: Optional[float]  # height/width of the largest blob's bounding box, None if empty


@dataclass(frozen=True)
class DetectionResult:
    detected: bool
    red_pct: float  # the largest blob's % of the region - what threshold_percent is compared against
    total_red_pct: float  # total mask %, for diagnostics (a "changed %" in infrared mode, not literally red)
    aspect_ratio: Optional[float]
    blob_pixel_count: int
    mode: str  # "color", "infrared", or "infrared-no-reference"


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


def _largest_blob(mask: List[List[bool]], width: int, height: int) -> Optional[Tuple[int, int, int, int, int]]:
    """Finds the largest 4-connected blob of True cells.

    Returns (pixel_count, min_x, min_y, max_x, max_y), or None if the mask is empty.
    """
    visited = [[False] * width for _ in range(height)]
    best: Optional[Tuple[int, int, int, int, int]] = None

    for start_y in range(height):
        for start_x in range(width):
            if not mask[start_y][start_x] or visited[start_y][start_x]:
                continue

            visited[start_y][start_x] = True
            queue = deque([(start_x, start_y)])
            pixel_count = 0
            min_x = max_x = start_x
            min_y = max_y = start_y

            while queue:
                x, y = queue.popleft()
                pixel_count += 1
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < width and 0 <= ny < height and mask[ny][nx] and not visited[ny][nx]:
                        visited[ny][nx] = True
                        queue.append((nx, ny))

            if best is None or pixel_count > best[0]:
                best = (pixel_count, min_x, min_y, max_x, max_y)

    return best


def _analyze_mask(mask: List[List[bool]], width: int, height: int) -> BlobAnalysis:
    total = width * height
    if total == 0:
        return BlobAnalysis(total_pct=0.0, blob_pct=0.0, blob_pixel_count=0, aspect_ratio=None)

    total_true = sum(row.count(True) for row in mask)
    total_pct = 100.0 * total_true / total

    blob = _largest_blob(mask, width, height)
    if blob is None:
        return BlobAnalysis(total_pct=total_pct, blob_pct=0.0, blob_pixel_count=0, aspect_ratio=None)

    pixel_count, min_x, min_y, max_x, max_y = blob
    bbox_width = max_x - min_x + 1
    bbox_height = max_y - min_y + 1

    return BlobAnalysis(
        total_pct=total_pct,
        blob_pct=100.0 * pixel_count / total,
        blob_pixel_count=pixel_count,
        aspect_ratio=bbox_height / bbox_width,
    )


# --- Color (daylight) detection -------------------------------------------------


def _red_mask(cropped: Image.Image, thresholds: HsvThresholds) -> Tuple[List[List[bool]], int, int]:
    hsv = cropped.convert("HSV")
    width, height = hsv.size
    pixels = hsv.load()
    mask = [[_is_red(*pixels[x, y], thresholds) for x in range(width)] for y in range(height)]
    return mask, width, height


def analyze_red_blob(image: Image.Image, roi: Roi, thresholds: HsvThresholds) -> BlobAnalysis:
    cropped = crop_roi(image.convert("RGB"), roi)
    mask, width, height = _red_mask(cropped, thresholds)
    return _analyze_mask(mask, width, height)


def red_pixel_percent(image: Image.Image, roi: Roi, thresholds: HsvThresholds) -> float:
    """Total % of the ROI that is a red pixel, ignoring shape.

    Kept for the offline calibration tool; detect_red_bin uses the more
    reliable largest-blob percentage instead (see analyze_red_blob).
    """
    return analyze_red_blob(image, roi, thresholds).total_pct


def detect_red_bin(
    image_bytes: bytes,
    roi: Roi,
    thresholds: HsvThresholds,
    threshold_percent: float,
    min_aspect_ratio: float = 0.0,
    max_aspect_ratio: float = float("inf"),
) -> DetectionResult:
    """Detects the bin from the largest connected red blob in the ROI, not a raw pixel count.

    Comparing against the single largest connected blob (rather than total red
    pixels scattered anywhere in the ROI) rejects noise that a plain color
    count would miscount as the bin: a wet-pavement reflection, a sliver of a
    red car passing in the background, a few red leaves. The aspect-ratio
    bounds add a second filter against a blob shaped nothing like the bin
    (e.g. a thin streak of glare). Only usable in daylight/color mode - see
    detect_bin_by_change_multi for infrared/night mode.
    """
    image = Image.open(BytesIO(image_bytes))
    image.load()
    analysis = analyze_red_blob(image, roi, thresholds)

    shape_ok = (
        analysis.aspect_ratio is not None
        and min_aspect_ratio <= analysis.aspect_ratio <= max_aspect_ratio
    )
    detected = analysis.blob_pct >= threshold_percent and shape_ok

    return DetectionResult(
        detected=detected,
        red_pct=analysis.blob_pct,
        total_red_pct=analysis.total_pct,
        aspect_ratio=analysis.aspect_ratio,
        blob_pixel_count=analysis.blob_pixel_count,
        mode="color",
    )


# --- Infrared (night) mode detection ---------------------------------------------


def average_saturation(image: Image.Image, sample_size: int = 80) -> float:
    """Average HSV saturation (0-255) across the whole image, downsampled for speed.

    A monochrome infrared image has ~0 saturation everywhere; a daylight
    color image does not, even if the ROI itself is a dull color - this is
    checked against the whole frame (not just the ROI) precisely so it
    doesn't depend on the ROI's own saturation.
    """
    width, height = image.size
    if width == 0 or height == 0:
        return 0.0
    scale = min(1.0, sample_size / max(width, height))
    if scale < 1.0:
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    saturation_values = list(image.convert("RGB").convert("HSV").getdata(band=1))
    return sum(saturation_values) / len(saturation_values) if saturation_values else 0.0


def is_infrared_mode(image: Image.Image, saturation_threshold: float = 15.0) -> bool:
    return average_saturation(image) < saturation_threshold


def is_infrared_snapshot(image_bytes: bytes, saturation_threshold: float = 15.0) -> bool:
    image = Image.open(BytesIO(image_bytes))
    image.load()
    return is_infrared_mode(image, saturation_threshold)


def _zscore_normalize(values: List[int]) -> List[float]:
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    std = variance ** 0.5 or 1.0  # avoid div-by-zero on a perfectly flat crop
    return [(v - mean) / std for v in values]


def _normalized_luminance(crop: Image.Image) -> List[float]:
    return _zscore_normalize(list(crop.convert("L").getdata()))


def _pixel_mean_std(samples: List[List[float]]) -> Tuple[List[float], List[float]]:
    """Per-index mean and stddev across several equal-length sample lists.

    Used to build the infrared baseline from multiple reference snapshots:
    each list is one reference's normalized luminance, so index i's mean/std
    describe how that one pixel position has historically looked/varied.
    """
    n = len(samples)
    length = len(samples[0]) if samples else 0
    means = [0.0] * length
    stds = [0.0] * length
    for i in range(length):
        values = [sample[i] for sample in samples]
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        means[i] = mean
        stds[i] = variance ** 0.5
    return means, stds


def _change_mask_adaptive(
    current_norm: List[float],
    pixel_mean: List[float],
    pixel_std: List[float],
    width: int,
    height: int,
    change_zscore: float,
    min_pixel_std: float,
) -> List[List[bool]]:
    """Flags a pixel as changed when it deviates from its own historical
    baseline by more than `change_zscore` multiples of that pixel's own
    historical stddev (floored at `min_pixel_std` so a pixel that has never
    varied across the reference samples doesn't become infinitely sensitive).
    """
    mask: List[List[bool]] = []
    idx = 0
    for _y in range(height):
        row = []
        for _x in range(width):
            std = max(pixel_std[idx], min_pixel_std)
            row.append(abs(current_norm[idx] - pixel_mean[idx]) / std >= change_zscore)
            idx += 1
        mask.append(row)
    return mask


def analyze_change_blob_multi(
    current: Image.Image,
    references: List[Image.Image],
    roi: Roi,
    change_zscore: float,
    min_pixel_std: float = 1.0,
) -> BlobAnalysis:
    if not references:
        raise ValueError("At least one reference snapshot is required")

    current_crop = crop_roi(current.convert("RGB"), roi)
    width, height = current_crop.size
    current_norm = _normalized_luminance(current_crop)

    reference_samples = []
    for reference in references:
        reference_crop = crop_roi(reference.convert("RGB"), roi)
        if reference_crop.size != current_crop.size:
            reference_crop = reference_crop.resize(current_crop.size)
        reference_samples.append(_normalized_luminance(reference_crop))

    pixel_mean, pixel_std = _pixel_mean_std(reference_samples)
    mask = _change_mask_adaptive(current_norm, pixel_mean, pixel_std, width, height, change_zscore, min_pixel_std)
    return _analyze_mask(mask, width, height)


def detect_bin_by_change_multi(
    image_bytes: bytes,
    reference_bytes_list: List[bytes],
    roi: Roi,
    threshold_percent: float,
    change_zscore: float = 1.0,
    min_pixel_std: float = 1.0,
    min_aspect_ratio: float = 0.0,
    max_aspect_ratio: float = float("inf"),
) -> DetectionResult:
    """Infrared/night detection: color is unavailable, so this looks for the
    largest blob of pixels in the ROI whose normalized luminance deviates
    from a per-pixel baseline (mean + stddev) built from several reference
    snapshots taken with the bin confirmed absent (see is_infrared_mode /
    is_infrared_snapshot to decide when to use this instead of
    detect_red_bin). A single reference snapshot still works - every pixel's
    stddev is then 0, floored to min_pixel_std (whose default of 1.0
    reproduces the original single-reference-only comparison exactly) - but
    several samples taken on different nights let genuinely noisy spots earn
    a wider tolerance instead of every pixel sharing one fixed threshold.
    """
    if not reference_bytes_list:
        raise ValueError("At least one reference snapshot is required")

    image = Image.open(BytesIO(image_bytes))
    image.load()
    references = []
    for reference_bytes in reference_bytes_list:
        reference = Image.open(BytesIO(reference_bytes))
        reference.load()
        references.append(reference)

    analysis = analyze_change_blob_multi(image, references, roi, change_zscore, min_pixel_std)

    shape_ok = (
        analysis.aspect_ratio is not None
        and min_aspect_ratio <= analysis.aspect_ratio <= max_aspect_ratio
    )
    detected = analysis.blob_pct >= threshold_percent and shape_ok

    return DetectionResult(
        detected=detected,
        red_pct=analysis.blob_pct,
        total_red_pct=analysis.total_pct,
        aspect_ratio=analysis.aspect_ratio,
        blob_pixel_count=analysis.blob_pixel_count,
        mode="infrared",
    )
