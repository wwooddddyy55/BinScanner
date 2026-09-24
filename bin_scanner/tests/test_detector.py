from io import BytesIO

from PIL import Image, ImageDraw

from detector import HsvThresholds, Roi, detect_red_bin, red_pixel_percent

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


def test_detects_red_bin_above_threshold():
    # Red square covering ~20% of a 100x100 frame.
    image_bytes = make_image_bytes(red_box=(0, 0, 44, 44))

    detected, red_pct = detect_red_bin(image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 10.0)

    assert detected is True
    assert 15.0 < red_pct < 25.0


def test_ignores_small_red_patch_below_threshold():
    # Red square covering ~4% of the frame - below the 10% threshold.
    image_bytes = make_image_bytes(red_box=(0, 0, 19, 19))

    detected, red_pct = detect_red_bin(image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 10.0)

    assert detected is False
    assert red_pct < 10.0


def test_no_red_present():
    image_bytes = make_image_bytes()

    detected, red_pct = detect_red_bin(image_bytes, FULL_FRAME_ROI, DEFAULT_THRESHOLDS, 10.0)

    assert detected is False
    assert red_pct == 0.0


def test_roi_restricts_detection_to_configured_region():
    # Red square only in the top-left quadrant; ROI covers only the bottom-right quadrant.
    image_bytes = make_image_bytes(size=(100, 100), red_box=(0, 0, 49, 49))
    bottom_right_roi = Roi(x=0.5, y=0.5, width=0.5, height=0.5)

    pct = red_pixel_percent(Image.open(BytesIO(image_bytes)), bottom_right_roi, DEFAULT_THRESHOLDS)

    assert pct == 0.0
