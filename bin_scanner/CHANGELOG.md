# Changelog

## 0.4.0

- Added infrared/night detection mode: since color is unavailable once the
  camera switches to IR illuminators, detection now auto-detects this from
  the snapshot's overall saturation and, in that mode, looks for the
  largest blob that changed against a stored "bin confirmed absent"
  reference photo instead of a color threshold. New options
  `ir_saturation_threshold`, `ir_change_threshold_percent`, and
  `ir_change_zscore`; capture/clear the reference from new
  `/calibrate/ir-reference` endpoints (also exposed in the `/calibrate` UI).
- If infrared mode is detected but no night reference has been captured yet,
  `/scan` now fails *safe toward `detected: true`* (sends the notification)
  rather than silently reporting "all clear" - for a bin-night reminder, a
  missed detection is a worse outcome than one extra notification.
- `/scan` and `/calibrate/preview` responses now include a `mode` field
  (`color`, `infrared`, or `infrared-no-reference`).
- `tools/calibrate.py` gained a `--reference` flag to test night detection
  offline, and auto-detects which mode a given snapshot is in.

## 0.3.0

- Switched default `camera_entity` and `roi_*` defaults from the doorbell to
  the garage/driveway camera, which frames the bin's storage spot without
  fisheye distortion or edge-cropping.
- Detection now finds the largest connected red blob in the region and
  checks its bounding-box aspect ratio (`min_aspect_ratio`/
  `max_aspect_ratio`), instead of just counting total red pixels - rejects
  scattered noise (glare, background clutter) and oddly-shaped red patches
  that a plain color-percentage count could miscount as the bin.
- `/scan` and `/calibrate/preview` responses now also include
  `total_red_pct` and `aspect_ratio` for diagnostics.

## 0.2.0

- Added a `/calibrate` web page: draw the detection region directly on the
  live camera snapshot and see the real detection result update as you
  adjust the box and color thresholds, instead of editing `roi_*` values
  blind.

## 0.1.0

- Initial release: HTTP `/scan` endpoint that fetches a Home Assistant camera
  snapshot, checks a configurable region of interest for red-bin-lid colored
  pixels, and calls a `notify.*` service when the bin is still detected.
