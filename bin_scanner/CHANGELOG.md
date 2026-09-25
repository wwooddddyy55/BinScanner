# Changelog

## 0.2.0

- Added a `/calibrate` web page: draw the detection region directly on the
  live camera snapshot and see the real detection result update as you
  adjust the box and color thresholds, instead of editing `roi_*` values
  blind.

## 0.1.0

- Initial release: HTTP `/scan` endpoint that fetches a Home Assistant camera
  snapshot, checks a configurable region of interest for red-bin-lid colored
  pixels, and calls a `notify.*` service when the bin is still detected.
