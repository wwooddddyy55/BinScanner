from io import BytesIO

from PIL import Image, ImageDraw

from detector import (
    HsvThresholds,
    Roi,
    detect_bin_by_change,
    detect_red_bin,
    is_infrared_mode,
    red_pixel_percent,
)

DEFAULT_THRESHOLDS = HsvThresholds(
    hue_min=0, hue_max=12, hue_min2=245, hue_max2=255, min_saturation=90, min_value=60
)
FULL_FRAME_ROI = Roi(x=0.0, y=0.0, width=1.0, height=1.0)


def make_image_bytes(size=(100, 100), bg=(120, 120, 120), red_box=None) -> bytes:
    image = Image.new("RGB", size, bg)
    if red_box is not None:
        ImageDraw.Draw(image).rectangle(red_box, fill=(220, 20, 20))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def make_scattered_noise_bytes(size=(100, 100), bg=(120, 120, 120), count=60, box_size=5, step=10) -> bytes:
    """Many small, non-touching red squares - no single blob is large, even though the
    total red pixel count adds up to well above a typical threshold."""
    image = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(image)
    placed = 0
    for row in range(0, size[1], step):
        for col in range(0, size[0], step):
            if placed >= count:
                break
            draw.rectangle((col, row, col + box_size - 1, row + box_size - 1), fill=(220, 20, 20))
            placed += 1
        if placed >= count:
            break
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_detects_red_bin_above_threshold():
    # Red square covering ~20% of a 100x100 frame.
    image_bytes = make_image_bytes(red_box=(0, 0, 44, 44))

    result = detect_red_bin(image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 10.0)

    assert result.detected is True
    assert 15.0 < result.red_pct < 25.0
    assert result.aspect_ratio == 1.0


def test_ignores_small_red_patch_below_threshold():
    # Red square covering ~4% of the frame - below the 10% threshold.
    image_bytes = make_image_bytes(red_box=(0, 0, 19, 19))

    result = detect_red_bin(image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 10.0)

    assert result.detected is False
    assert result.red_pct < 10.0


def test_no_red_present():
    image_bytes = make_image_bytes()

    result = detect_red_bin(image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 10.0)

    assert result.detected is False
    assert result.red_pct == 0.0
    assert result.aspect_ratio is None


def test_scattered_noise_does_not_trigger_detection():
    # 60 isolated 5x5 red squares = 15% of the frame in total, but no single
    # connected blob is anywhere near that size - the blob-based check should
    # reject this even though a naive total-pixel-count check would not.
    image_bytes = make_scattered_noise_bytes()

    result = detect_red_bin(image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 10.0)

    assert result.total_red_pct > 10.0
    assert result.red_pct < 1.0
    assert result.detected is False


def test_rejects_blob_with_wrong_aspect_ratio():
    # A wide, thin red streak (80x5 = 4% of the frame) clears the area
    # threshold but looks nothing like the bin - the aspect-ratio filter
    # should reject it.
    image_bytes = make_image_bytes(size=(100, 100), red_box=(0, 0, 79, 4))

    unfiltered = detect_red_bin(image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 2.0)
    filtered = detect_red_bin(
        image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 2.0, min_aspect_ratio=0.3, max_aspect_ratio=4.0
    )

    assert unfiltered.detected is True  # sanity check: area alone would pass
    assert filtered.detected is False
    assert filtered.aspect_ratio < 0.3


def test_roi_restricts_detection_to_configured_region():
    # Red square only in the top-left quadrant; ROI covers only the bottom-right quadrant.
    image_bytes = make_image_bytes(size=(100, 100), red_box=(0, 0, 49, 49))
    bottom_right_roi = Roi(x=0.5, y=0.5, width=0.5, height=0.5)

    pct = red_pixel_percent(Image.open(BytesIO(image_bytes)), bottom_right_roi, DEFAULT_THRESHOLDS)

    assert pct == 0.0


# --- Infrared (night) mode --------------------------------------------------


def make_grayscale_bytes(size=(100, 100), bg=128, patch=None, patch_value=220) -> bytes:
    image = Image.new("L", size, bg).convert("RGB")
    if patch is not None:
        image_draw = ImageDraw.Draw(image)
        image_draw.rectangle(patch, fill=(patch_value, patch_value, patch_value))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_is_infrared_mode_distinguishes_monochrome_from_color():
    gray_image = Image.open(BytesIO(make_grayscale_bytes()))
    color_image = Image.open(BytesIO(make_image_bytes(red_box=(0, 0, 44, 44))))

    assert is_infrared_mode(gray_image) is True
    assert is_infrared_mode(color_image) is False


def test_detect_bin_by_change_flags_new_object_against_reference():
    reference_bytes = make_grayscale_bytes()  # empty driveway at night
    current_bytes = make_grayscale_bytes(patch=(20, 20, 60, 60))  # bin now present

    result = detect_bin_by_change(current_bytes, reference_bytes, FULL_FRAME_ROI, threshold_percent=10.0)

    assert result.mode == "infrared"
    assert result.detected is True
    assert result.aspect_ratio == 1.0


def test_detect_bin_by_change_ignores_identical_snapshot():
    reference_bytes = make_grayscale_bytes()
    current_bytes = make_grayscale_bytes()  # nothing changed

    result = detect_bin_by_change(current_bytes, reference_bytes, FULL_FRAME_ROI, threshold_percent=10.0)

    assert result.detected is False
    assert result.red_pct == 0.0


def test_detect_bin_by_change_tolerates_uniform_brightness_shift():
    # Global exposure/IR illuminator brightness can vary night to night with
    # nothing actually different in frame - z-score normalization should
    # absorb a uniform shift rather than flagging the whole frame as changed.
    reference_bytes = make_grayscale_bytes(bg=128)
    brighter_bytes = make_grayscale_bytes(bg=160)

    result = detect_bin_by_change(brighter_bytes, reference_bytes, FULL_FRAME_ROI, threshold_percent=10.0)

    assert result.detected is False


def test_detect_bin_by_change_respects_aspect_ratio_filter():
    reference_bytes = make_grayscale_bytes()
    # A thin horizontal streak of change (e.g. glare) rather than a bin-shaped blob.
    current_bytes = make_grayscale_bytes(patch=(0, 0, 79, 4))

    unfiltered = detect_bin_by_change(current_bytes, reference_bytes, FULL_FRAME_ROI, threshold_percent=2.0)
    filtered = detect_bin_by_change(
        current_bytes, reference_bytes, FULL_FRAME_ROI, threshold_percent=2.0,
        min_aspect_ratio=0.3, max_aspect_ratio=4.0,
    )

    assert unfiltered.detected is True
    assert filtered.detected is False
