# Changelog

## 0.5.1

- `/calibrate` is now mobile-friendly: added a viewport meta tag, touch
  support for drawing the ROI box (drag-to-select now works with a finger,
  not just a mouse), and a responsive layout that stacks cleanly on a phone
  screen instead of relying on a fixed-width desktop layout.
- Added a `webui` entry to `config.yaml` pointing at `/calibrate`, so Home
  Assistant shows an **OPEN WEB UI** button straight to the calibration page
  from the add-on's own page (next to the Configuration tab).

## 0.5.0

- Infrared/night detection now builds its baseline from **multiple** "bin
  confirmed absent" reference samples instead of one fixed image: for each
  pixel it learns a mean and stddev across the captured samples, so a spot
  that's normally noisy from night to night (streetlight cycling, moonlight,
  IR-illuminator gain drift) gets a proportionally wider tolerance instead of
  every pixel sharing one fixed threshold. A single sample still works (falls
  back to the new `ir_min_pixel_std` floor, close to the old behavior).
  `detect_bin_by_change`/`analyze_change_blob` are replaced by
  `detect_bin_by_change_multi`/`analyze_change_blob_multi`; `tools/calibrate.py`'s
  `--reference` flag is now repeatable.
- The night reference is now a rolling set of samples captured one at a time
  from `/calibrate` (new `ir_reference_max_samples` option, default 5; the
  oldest sample is dropped automatically past that count) instead of a single
  file, so re-capturing periodically both grows the baseline and keeps it
  current as the scene drifts (dirt, leaves, seasons).
- Added an optional confirmation debounce: `confirm_consecutive_scans` (only
  useful if an HA automation scans more than once per evening) requires that
  many consecutive `detected: true` scans before notifying, guarding against
  a one-off false trigger (a headlight sweep, a passing shadow) firing the
  alert; `confirm_max_gap_minutes` resets the streak if scans are too far
  apart to plausibly be the same evening. Defaults to `1` (off) to preserve
  existing single-scan-per-night behavior. The fail-safe
  `infrared-no-reference` case always notifies immediately regardless, since
  it's a configuration gap rather than a noisy measurement.
- `/scan` responses now include a `confirmed` field - what actually gates the
  notification, distinct from the raw `detected` result of this one scan.
  `/calibrate/preview` is unaffected (it tests detection directly, without the
  scan-history-dependent debounce).

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
