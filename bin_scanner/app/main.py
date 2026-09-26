"""BinScanner add-on HTTP service.

Exposes a single action endpoint (POST /scan) that an HA automation calls on
a schedule. The container otherwise sits idle between calls.
"""
from __future__ import annotations

import json
import logging
import os
import time
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
    detect_bin_by_change_multi,
    detect_red_bin,
    is_infrared_snapshot,
)
from ha_client import HomeAssistantError, call_notify_service, fetch_camera_snapshot

OPTIONS_PATH = Path(os.environ.get("BIN_SCANNER_OPTIONS", "/data/options.json"))
CALIBRATE_HTML_PATH = Path(__file__).parent / "calibrate.html"
IR_REFERENCE_DIR = Path(os.environ.get("BIN_SCANNER_IR_REFERENCE_DIR", "/data/ir_reference"))
SCAN_STATE_PATH = Path(os.environ.get("BIN_SCANNER_SCAN_STATE", "/data/scan_state.json"))


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


def _ir_reference_files() -> list[Path]:
    if not IR_REFERENCE_DIR.exists():
        return []
    return sorted(IR_REFERENCE_DIR.glob("*.jpg"))


def load_ir_references() -> list[bytes]:
    """Loads all captured "bin absent" night reference samples, oldest first.

    Several samples (ideally captured on different nights) let detect_bin_by_change_multi
    build a per-pixel baseline instead of comparing against one fixed image -
    see detector.py's module docstring for why that matters.
    """
    return [f.read_bytes() for f in _ir_reference_files()]


def run_detection(image_bytes: bytes) -> DetectionResult:
    """Picks color or infrared detection based on the snapshot itself, not the clock.

    Infrared/night mode is where the camera's IR illuminators are on and the
    image is monochrome - there's no color left to threshold, so this falls
    back to comparing against stored "bin confirmed absent" reference samples
    instead (see detect_bin_by_change_multi). If no reference has been
    captured yet, this fails *safe toward detected=True*: for a "did you
    forget the bin" reminder, a missed detection (silently reporting "all
    clear" when it's still there) is a much worse outcome than one extra
    notification.
    """
    if is_infrared_snapshot(image_bytes, OPTIONS["ir_saturation_threshold"]):
        references = load_ir_references()
        if not references:
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
        return detect_bin_by_change_multi(
            image_bytes,
            references,
            ROI,
            OPTIONS["ir_change_threshold_percent"],
            OPTIONS["ir_change_zscore"],
            OPTIONS["ir_min_pixel_std"],
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


def _load_scan_state() -> dict:
    if not SCAN_STATE_PATH.exists():
        return {"streak": 0, "last_detected_at": None}
    try:
        return json.loads(SCAN_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"streak": 0, "last_detected_at": None}


def _save_scan_state(state: dict) -> None:
    SCAN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCAN_STATE_PATH.write_text(json.dumps(state))


def update_confirmation(detected: bool, now: datetime) -> bool:
    """Tracks consecutive detected=True scans and reports whether the streak has
    reached `confirm_consecutive_scans` - i.e. whether this is trustworthy enough
    to notify on, not just a single stray frame (headlight sweep, a passing
    shadow, a cat).

    A gap longer than `confirm_max_gap_minutes` since the last detected=True
    scan resets the streak, so a leftover streak from a previous night can't
    silently satisfy tonight's confirmation count. With the default
    confirm_consecutive_scans=1 this is a no-op passthrough - the debounce
    only kicks in if the HA automation is set up to scan multiple times per
    evening.
    """
    required = OPTIONS.get("confirm_consecutive_scans", 1)
    if required <= 1:
        return detected

    if not detected:
        _save_scan_state({"streak": 0, "last_detected_at": None})
        return False

    state = _load_scan_state()
    streak = state.get("streak", 0)
    last_at = state.get("last_detected_at")
    max_gap_minutes = OPTIONS.get("confirm_max_gap_minutes", 180)
    if last_at:
        try:
            last_dt = datetime.fromisoformat(last_at)
        except ValueError:
            last_dt = None
        if last_dt is None or (now - last_dt).total_seconds() > max_gap_minutes * 60:
            streak = 0

    streak += 1
    _save_scan_state({"streak": streak, "last_detected_at": now.isoformat()})
    return streak >= required


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
        ir_min_pixel_std=OPTIONS["ir_min_pixel_std"],
        ir_reference_max_samples=OPTIONS["ir_reference_max_samples"],
        confirm_consecutive_scans=OPTIONS["confirm_consecutive_scans"],
        confirm_max_gap_minutes=OPTIONS["confirm_max_gap_minutes"],
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
    files = _ir_reference_files()
    captured_ats = [
        datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat() for f in files
    ]
    return jsonify(
        exists=len(files) > 0,
        count=len(files),
        max_samples=OPTIONS["ir_reference_max_samples"],
        captured_at=captured_ats[-1] if captured_ats else None,
        captured_ats=captured_ats,
    )


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
    IR_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    # time_ns for ordering/uniqueness; the loop guards the (astronomically
    # unlikely, but cheap to rule out) case of two captures landing in the
    # same nanosecond.
    candidate = IR_REFERENCE_DIR / f"{time.time_ns()}.jpg"
    while candidate.exists():
        candidate = IR_REFERENCE_DIR / f"{time.time_ns()}.jpg"
    candidate.write_bytes(_last_calibration_snapshot)

    # Keep only the most recent N samples so the baseline tracks recent
    # conditions (seasons, dirt, camera repositioning) instead of growing
    # forever or being dragged down by a stale sample from months ago.
    files = _ir_reference_files()
    max_samples = OPTIONS["ir_reference_max_samples"]
    for stale in files[:-max_samples] if len(files) > max_samples else []:
        stale.unlink(missing_ok=True)

    count = min(len(files), max_samples)
    logger.info("Captured new infrared reference snapshot (%d/%d samples)", count, max_samples)
    return jsonify(status="ok", count=count)


@app.delete("/calibrate/ir-reference")
def calibrate_delete_ir_reference():
    for f in _ir_reference_files():
        f.unlink(missing_ok=True)
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
            min_pixel_std = float(body.get("ir_min_pixel_std", OPTIONS["ir_min_pixel_std"]))
        except (ValueError, TypeError) as exc:
            return jsonify(error=f"Invalid parameters: {exc}"), 400

        references = load_ir_references()
        if not references:
            return (
                jsonify(
                    error="Infrared mode detected but no night reference captured yet - "
                    "see the Night reference panel below",
                    mode="infrared-no-reference",
                ),
                400,
            )
        try:
            result = detect_bin_by_change_multi(
                _last_calibration_snapshot,
                references,
                roi,
                threshold_percent,
                change_zscore,
                min_pixel_std,
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

    # The "no reference yet" fail-safe should notify immediately, not wait out
    # a confirmation streak - it isn't a noisy image measurement, it's a
    # standing configuration gap that should be surfaced right away.
    if result.mode == "infrared-no-reference":
        confirmed = result.detected
    else:
        confirmed = update_confirmation(result.detected, datetime.now(timezone.utc))

    logger.info(
        "Scan result: detected=%s confirmed=%s mode=%s blob_pct=%.2f%% total_pct=%.2f%% aspect_ratio=%s dry_run=%s",
        result.detected,
        confirmed,
        result.mode,
        result.red_pct,
        result.total_red_pct,
        result.aspect_ratio,
        dry_run,
    )

    notified = False
    if confirmed and not dry_run:
        try:
            call_notify_service(
                OPTIONS["notify_service"], OPTIONS["notify_title"], OPTIONS["notify_message"]
            )
            notified = True
        except HomeAssistantError as exc:
            logger.error("Notify failed: %s", exc)
            return (
                jsonify(
                    detected=result.detected,
                    confirmed=confirmed,
                    red_pct=round(result.red_pct, 2),
                    notified=False,
                    error=str(exc),
                ),
                502,
            )

    return jsonify(
        detected=result.detected,
        confirmed=confirmed,
        red_pct=round(result.red_pct, 2),
        mode=result.mode,
        notified=notified,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8099"))
    app.run(host="0.0.0.0", port=port)
