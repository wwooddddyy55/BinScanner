"""BinScanner add-on HTTP service.

Exposes a single action endpoint (POST /scan) that an HA automation calls on
a schedule. The container otherwise sits idle between calls.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request

from detector import HsvThresholds, Roi, detect_red_bin
from ha_client import HomeAssistantError, call_notify_service, fetch_camera_snapshot

OPTIONS_PATH = Path(os.environ.get("BIN_SCANNER_OPTIONS", "/data/options.json"))
CALIBRATE_HTML_PATH = Path(__file__).parent / "calibrate.html"


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
        detected, red_pct = detect_red_bin(_last_calibration_snapshot, roi, thresholds, threshold_percent)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(detected=detected, red_pct=round(red_pct, 2))


@app.post("/scan")
def scan():
    dry_run = request.args.get("dry_run", "false").lower() == "true"

    try:
        image_bytes = fetch_camera_snapshot(OPTIONS["camera_entity"])
        detected, red_pct = detect_red_bin(
            image_bytes, ROI, THRESHOLDS, OPTIONS["red_pixel_threshold_percent"]
        )
    except HomeAssistantError as exc:
        logger.error("Scan failed: %s", exc)
        return jsonify(error=str(exc)), 502
    except Exception as exc:  # malformed/undecodable snapshot, etc.
        logger.exception("Unexpected error during scan")
        return jsonify(error=str(exc)), 500

    logger.info("Scan result: detected=%s red_pct=%.2f%% dry_run=%s", detected, red_pct, dry_run)

    notified = False
    if detected and not dry_run:
        try:
            call_notify_service(
                OPTIONS["notify_service"], OPTIONS["notify_title"], OPTIONS["notify_message"]
            )
            notified = True
        except HomeAssistantError as exc:
            logger.error("Notify failed: %s", exc)
            return (
                jsonify(detected=detected, red_pct=round(red_pct, 2), notified=False, error=str(exc)),
                502,
            )

    return jsonify(detected=detected, red_pct=round(red_pct, 2), notified=notified, dry_run=dry_run)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8099"))
    app.run(host="0.0.0.0", port=port)
