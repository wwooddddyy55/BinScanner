# Changelog

## 0.1.0

- Initial release: HTTP `/scan` endpoint that fetches a Home Assistant camera
  snapshot, checks a configurable region of interest for red-bin-lid colored
  pixels, and calls a `notify.*` service when the bin is still detected.
