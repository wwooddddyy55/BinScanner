"""BinScanner add-on HTTP service.

Exposes a single action endpoint (POST /scan) that an HA automation calls on
a schedule. The container otherwise sits idle between calls.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request

from io import BytesIO

from PIL import Image

from detector import (
    DetectionResult,
    HsvThresholds,
    Roi,
    average_saturation,
    detect_bin_by_change,
    detect_red_bin,
    is_infrared_snapshot,
)
from ha_client import HomeAssistantError, call_notify_service, fetch_camera_snapshot

OPTIONS_PATH = Path(os.environ.get("BIN_SCANNER_OPTIONS", "/data/options.json"))
CALIBRATE_HTML_PATH = Path(__file__).parent / "calibrate.html"
IR_REFERENCE_PATH = Path(os.environ.get("BIN_SCANNER_IR_REFERENCE", "/data/ir_reference.jpg"))


def load_options() -> dict:
    with OPTIONS_PATH.open() as f:
        return json.load(f)


OPTIONS = load_options()

logging.basicConfig(
    level=getattr(logging, OPTIONS.get("log_level", "info").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bin_scanner")

ROI = Roi(
    x=OPTIONS["roi_x"],
    y=OPTIONS["roi_y"],
    width=OPTIONS["roi_width"],
    height=OPTIONS["roi_height"],
)
THRESHOLDS = HsvThresholds(
    hue_min=OPTIONS["red_hue_min"],
    hue_max=OPTIONS["red_hue_max"],
    hue_min2=OPTIONS["red_hue_min2"],
    hue_max2=OPTIONS["red_hue_max2"],
    min_saturation=OPTIONS["min_saturation"],
    min_value=OPTIONS["min_value"],
)

app = Flask(__name__)

# Cache of the most recent snapshot fetched via the /calibrate UI, so the
# preview endpoint can re-run detection against exactly what's on screen
# without re-fetching (and without the two ever drifting apart).
_last_calibration_snapshot: bytes | None = None


def load_ir_reference() -> bytes | None:
    if not IR_REFERENCE_PATH.exists():
        return None
    return IR_REFERENCE_PATH.read_bytes()


def run_detection(image_bytes: bytes) -> DetectionResult:
    """Picks color or infrared detection based on the snapshot itself, not the clock.

    Infrared/night mode is where the camera's IR illuminators are on and the
    image is monochrome - there's no color left to threshold, so this falls
    back to comparing against a stored "bin confirmed absent" reference
    instead (see detect_bin_by_change). If no reference has been captured
    yet, this fails *safe toward detected=True*: for a "did you forget the
    bin" reminder, a missed detection (silently reporting "all clear" when
    it's still there) is a much worse outcome than one extra notification.
    """
    if is_infrared_snapshot(image_bytes, OPTIONS["ir_saturation_threshold"]):
        reference_bytes = load_ir_reference()
        if reference_bytes is None:
            logger.warning(
                "Infrared mode detected but no night reference captured yet - "
                "defaulting to detected=True. Capture one via /calibrate while it's "
                "dark and the bin is confirmed absent."
            )
            return DetectionResult(
                detected=True,
                red_pct=0.0,
                total_red_pct=0.0,
                aspect_ratio=None,
                blob_pixel_count=0,
                mode="infrared-no-reference",
            )
        return detect_bin_by_change(
            image_bytes,
            reference_bytes,
            ROI,
            OPTIONS["ir_change_threshold_percent"],
            OPTIONS["ir_change_zscore"],
            OPTIONS["min_aspect_ratio"],
            OPTIONS["max_aspect_ratio"],
        )

    return detect_red_bin(
        image_bytes,
        ROI,
        THRESHOLDS,
        OPTIONS["red_pixel_threshold_percent"],
        OPTIONS["min_aspect_ratio"],
        OPTIONS["max_aspect_ratio"],
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/calibrate")
def calibrate_page():
    return Response(CALIBRATE_HTML_PATH.read_text(), mimetype="text/html")


@app.get("/calibrate/options")
def calibrate_options():
    return jsonify(
        roi_x=OPTIONS["roi_x"],
        roi_y=OPTIONS["roi_y"],
        roi_width=OPTIONS["roi_width"],
        roi_height=OPTIONS["roi_height"],
        red_hue_min=OPTIONS["red_hue_min"],
        red_hue_max=OPTIONS["red_hue_max"],
        red_hue_min2=OPTIONS["red_hue_min2"],
        red_hue_max2=OPTIONS["red_hue_max2"],
        min_saturation=OPTIONS["min_saturation"],
        min_value=OPTIONS["min_value"],
        red_pixel_threshold_percent=OPTIONS["red_pixel_threshold_percent"],
        min_aspect_ratio=OPTIONS["min_aspect_ratio"],
        max_aspect_ratio=OPTIONS["max_aspect_ratio"],
        ir_saturation_threshold=OPTIONS["ir_saturation_threshold"],
        ir_change_threshold_percent=OPTIONS["ir_change_threshold_percent"],
        ir_change_zscore=OPTIONS["ir_change_zscore"],
    )


@app.get("/calibrate/snapshot")
def calibrate_snapshot():
    global _last_calibration_snapshot
    try:
        _last_calibration_snapshot = fetch_camera_snapshot(OPTIONS["camera_entity"])
    except HomeAssistantError as exc:
        logger.error("Calibration snapshot fetch failed: %s", exc)
        return jsonify(error=str(exc)), 502
    return Response(_last_calibration_snapshot, mimetype="image/jpeg")


@app.get("/calibrate/ir-reference")
def calibrate_ir_reference_status():
    exists = IR_REFERENCE_PATH.exists()
    captured_at = None
    if exists:
        captured_at = datetime.fromtimestamp(IR_REFERENCE_PATH.stat().st_mtime, tz=timezone.utc).isoformat()
    return jsonify(exists=exists, captured_at=captured_at)


@app.post("/calibrate/ir-reference")
def calibrate_capture_ir_reference():
    if _last_calibration_snapshot is None:
        return jsonify(error="No snapshot loaded yet - click Refresh snapshot first"), 400
    if not is_infrared_snapshot(_last_calibration_snapshot, OPTIONS["ir_saturation_threshold"]):
        return (
            jsonify(
                error="Current snapshot doesn't look like infrared/night mode - "
                "wait until it's dark (and the bin is confirmed absent) before capturing"
            ),
            400,
        )
    IR_REFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    IR_REFERENCE_PATH.write_bytes(_last_calibration_snapshot)
    logger.info("Captured new infrared reference snapshot")
    return jsonify(status="ok")


@app.delete("/calibrate/ir-reference")
def calibrate_delete_ir_reference():
    IR_REFERENCE_PATH.unlink(missing_ok=True)
    return jsonify(status="ok")


@app.post("/calibrate/preview")
def calibrate_preview():
    if _last_calibration_snapshot is None:
        return jsonify(error="No snapshot loaded yet - click Refresh snapshot first"), 400

    body = request.get_json(silent=True) or {}
    try:
        roi = Roi(
            x=float(body["roi_x"]),
            y=float(body["roi_y"]),
            width=float(body["roi_width"]),
            height=float(body["roi_height"]),
        )
        min_aspect_ratio = float(body.get("min_aspect_ratio", 0.0))
        max_aspect_ratio = float(body.get("max_aspect_ratio", float("inf")))
        ir_saturation_threshold = float(body.get("ir_saturation_threshold", OPTIONS["ir_saturation_threshold"]))
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify(error=f"Invalid parameters: {exc}"), 400

    infrared = is_infrared_snapshot(_last_calibration_snapshot, ir_saturation_threshold)

    if infrared:
        try:
            threshold_percent = float(body.get("ir_change_threshold_percent", OPTIONS["ir_change_threshold_percent"]))
            change_zscore = float(body.get("ir_change_zscore", OPTIONS["ir_change_zscore"]))
        except (ValueError, TypeError) as exc:
            return jsonify(error=f"Invalid parameters: {exc}"), 400

        reference_bytes = load_ir_reference()
        if reference_bytes is None:
            return (
                jsonify(
                    error="Infrared mode detected but no night reference captured yet - "
                    "see the Night reference panel below",
                    mode="infrared-no-reference",
                ),
                400,
            )
        try:
            result = detect_bin_by_change(
                _last_calibration_snapshot,
                reference_bytes,
                roi,
                threshold_percent,
                change_zscore,
                min_aspect_ratio,
                max_aspect_ratio,
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    else:
        try:
            thresholds = HsvThresholds(
                hue_min=int(body["red_hue_min"]),
                hue_max=int(body["red_hue_max"]),
                hue_min2=int(body["red_hue_min2"]),
                hue_max2=int(body["red_hue_max2"]),
                min_saturation=int(body["min_saturation"]),
                min_value=int(body["min_value"]),
            )
            threshold_percent = float(body["red_pixel_threshold_percent"])
        except (KeyError, ValueError, TypeError) as exc:
            return jsonify(error=f"Invalid parameters: {exc}"), 400
        try:
            result = detect_red_bin(
                _last_calibration_snapshot, roi, thresholds, threshold_percent, min_aspect_ratio, max_aspect_ratio
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    snapshot_image = Image.open(BytesIO(_last_calibration_snapshot))
    snapshot_image.load()

    return jsonify(
        detected=result.detected,
        red_pct=round(result.red_pct, 2),
        total_red_pct=round(result.total_red_pct, 2),
        aspect_ratio=round(result.aspect_ratio, 2) if result.aspect_ratio is not None else None,
        mode=result.mode,
        average_saturation=round(average_saturation(snapshot_image), 1),
    )


@app.post("/scan")
def scan():
    dry_run = request.args.get("dry_run", "false").lower() == "true"

    try:
        image_bytes = fetch_camera_snapshot(OPTIONS["camera_entity"])
        result = run_detection(image_bytes)
    except HomeAssistantError as exc:
        logger.error("Scan failed: %s", exc)
        return jsonify(error=str(exc)), 502
    except Exception as exc:  # malformed/undecodable snapshot, etc.
        logger.exception("Unexpected error during scan")
        return jsonify(error=str(exc)), 500

    logger.info(
        "Scan result: detected=%s mode=%s blob_pct=%.2f%% total_pct=%.2f%% aspect_ratio=%s dry_run=%s",
        result.detected,
        result.mode,
        result.red_pct,
        result.total_red_pct,
        result.aspect_ratio,
        dry_run,
    )

    notified = False
    if result.detected and not dry_run:
        try:
            call_notify_service(
                OPTIONS["notify_service"], OPTIONS["notify_title"], OPTIONS["notify_message"]
            )
            notified = True
        except HomeAssistantError as exc:
            logger.error("Notify failed: %s", exc)
            return (
                jsonify(detected=result.detected, red_pct=round(result.red_pct, 2), notified=False, error=str(exc)),
                502,
            )

    return jsonify(
        detected=result.detected,
        red_pct=round(result.red_pct, 2),
        mode=result.mode,
        notified=notified,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8099"))
    app.run(host="0.0.0.0", port=port)
