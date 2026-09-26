from io import BytesIO

from PIL import Image, ImageDraw

from detector import (
    HsvThresholds,
    Roi,
    _change_mask_adaptive,
    _pixel_mean_std,
    detect_bin_by_change_multi,
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

    result = detect_bin_by_change_multi(current_bytes, [reference_bytes], FULL_FRAME_ROI, threshold_percent=10.0)

    assert result.mode == "infrared"
    assert result.detected is True
    assert result.aspect_ratio == 1.0


def test_detect_bin_by_change_ignores_identical_snapshot():
    reference_bytes = make_grayscale_bytes()
    current_bytes = make_grayscale_bytes()  # nothing changed

    result = detect_bin_by_change_multi(current_bytes, [reference_bytes], FULL_FRAME_ROI, threshold_percent=10.0)

    assert result.detected is False
    assert result.red_pct == 0.0


def test_detect_bin_by_change_tolerates_uniform_brightness_shift():
    # Global exposure/IR illuminator brightness can vary night to night with
    # nothing actually different in frame - z-score normalization should
    # absorb a uniform shift rather than flagging the whole frame as changed.
    reference_bytes = make_grayscale_bytes(bg=128)
    brighter_bytes = make_grayscale_bytes(bg=160)

    result = detect_bin_by_change_multi(brighter_bytes, [reference_bytes], FULL_FRAME_ROI, threshold_percent=10.0)

    assert result.detected is False


def test_detect_bin_by_change_respects_aspect_ratio_filter():
    reference_bytes = make_grayscale_bytes()
    # A thin horizontal streak of change (e.g. glare) rather than a bin-shaped blob.
    current_bytes = make_grayscale_bytes(patch=(0, 0, 79, 4))

    unfiltered = detect_bin_by_change_multi(current_bytes, [reference_bytes], FULL_FRAME_ROI, threshold_percent=2.0)
    filtered = detect_bin_by_change_multi(
        current_bytes, [reference_bytes], FULL_FRAME_ROI, threshold_percent=2.0,
        min_aspect_ratio=0.3, max_aspect_ratio=4.0,
    )

    assert unfiltered.detected is True
    assert filtered.detected is False


def test_detect_bin_by_change_requires_at_least_one_reference():
    current_bytes = make_grayscale_bytes()
    try:
        detect_bin_by_change_multi(current_bytes, [], FULL_FRAME_ROI, threshold_percent=10.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_pixel_mean_std_learns_a_wider_spread_for_a_historically_noisy_pixel():
    # Pixel 0 varies a lot night to night (e.g. near a streetlight/moon
    # glare); pixel 1 never varies. Exercised directly on the normalized-
    # luminance building blocks - a real two-level test image can't isolate
    # this, since z-score normalizing a small flat patch against a uniform
    # background makes the normalized value depend only on the patch's pixel
    # count, not its actual intensity (see analyze_change_blob_multi's
    # image-level tests for that behavior instead).
    samples = [[0.5, 0.0], [-0.6, 0.05], [0.4, -0.05], [-0.5, 0.0]]

    pixel_mean, pixel_std = _pixel_mean_std(samples)

    assert pixel_std[0] > pixel_std[1]

    # Tonight: pixel 0 lands within its usual noisy range; pixel 1 shifts by
    # the same absolute amount, which is unprecedented for that stable pixel.
    current = [0.55, 0.5]
    mask = _change_mask_adaptive(
        current, pixel_mean, pixel_std, width=2, height=1, change_zscore=1.5, min_pixel_std=0.05
    )

    assert mask[0][0] is False  # noisy pixel: within its historical spread
    assert mask[0][1] is True  # stable pixel: same absolute shift is way outside its historical spread


def test_multi_reference_pipeline_handles_several_varying_brightness_samples():
    # Integration sanity check: several full-frame reference samples at
    # different (but each uniform) brightness levels - detect_bin_by_change_multi
    # should run end-to-end without error and correctly find no change, since
    # a uniform frame always normalizes to a flat 0 regardless of its actual
    # brightness (the noisy-pixel tolerance itself is exercised more directly
    # in the _pixel_mean_std/_change_mask_adaptive unit test above).
    references = [make_grayscale_bytes(bg=v) for v in (100, 160, 120, 150)]
    current_bytes = make_grayscale_bytes(bg=130)

    result = detect_bin_by_change_multi(current_bytes, references, FULL_FRAME_ROI, threshold_percent=2.0)

    assert result.detected is False


def test_adaptive_reference_still_flags_a_genuinely_new_object():
    # Same noisy corner as above, but a real bin-sized blob appears elsewhere
    # in the frame that never varied across any reference sample.
    noisy_patch = (0, 0, 19, 19)
    bin_patch = (40, 40, 80, 80)
    references = [
        make_grayscale_bytes(patch=noisy_patch, patch_value=100),
        make_grayscale_bytes(patch=noisy_patch, patch_value=160),
        make_grayscale_bytes(patch=noisy_patch, patch_value=120),
    ]
    current_bytes = make_grayscale_bytes(patch=bin_patch, patch_value=220)

    result = detect_bin_by_change_multi(
        current_bytes, references, FULL_FRAME_ROI, threshold_percent=2.0, change_zscore=1.0
    )

    assert result.detected is True


def test_single_reference_sample_falls_back_to_min_pixel_std_floor():
    # With only one reference sample every pixel's historical stddev is 0,
    # so min_pixel_std alone determines sensitivity - this should behave like
    # the original single-reference comparison, not divide-by-zero or become
    # infinitely sensitive.
    reference_bytes = make_grayscale_bytes()
    current_bytes = make_grayscale_bytes(patch=(20, 20, 60, 60))

    result = detect_bin_by_change_multi(
        current_bytes, [reference_bytes], FULL_FRAME_ROI, threshold_percent=10.0, min_pixel_std=0.2
    )

    assert result.detected is True
