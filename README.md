# BinScanner

A tiny Home Assistant add-on that checks a doorbell/camera snapshot once a
week for a red-lidded bin still sitting out front, and sends a push
notification if it's still there.

It's built for a low resource footprint: no ML model, no OpenCV/numpy — just
a Pillow color-threshold check over a small, configurable region of the
frame, triggered by Home Assistant's own scheduler rather than a container
that's constantly polling.

## How it works

```
HA automation (weekly time trigger)
  → rest_command → POST http://<addon-hostname>:8099/scan
      bin_scanner add-on:
        1. Fetches a snapshot from your existing camera entity via HA's API
        2. Crops the configured region of interest (where the bin sits)
        3. Counts "red" pixels in that region (HSV threshold)
        4. If enough red pixels are found → calls your notify.* service
```

The add-on authenticates to Home Assistant automatically via the
Supervisor-injected token (`homeassistant_api: true` in `config.yaml`) — no
long-lived access token to create or manage.

## 1. Install the add-on

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add `https://github.com/wwooddddyy55/binscanner` and refresh.
3. Find **Bin Scanner** in the store and install it.
4. In the add-on's **Configuration** tab, set at minimum:
   - `camera_entity`: your doorbell's camera entity, e.g. `camera.front_doorbell`
   - `notify_service`: your phone's notify service *without* the `notify.`
     prefix, e.g. `mobile_app_coreys_iphone` (find it under
     **Developer Tools → Actions**, search "notify")
   - `roi_x`, `roi_y`, `roi_width`, `roi_height`: the region of the frame
     where the bin normally sits (see calibration below)
5. Start the add-on and check its log for `Running on http://0.0.0.0:8099`.

## 2. Calibrate the detection region

Rather than guessing `roi_*`/threshold values by redeploying the add-on
repeatedly, save a real snapshot from the camera (e.g. from the camera
entity's "download" button in HA, or a still from the Reolink app) and run
the standalone calibration tool locally:

```bash
python3 -m pip install pillow
python3 tools/calibrate.py doorbell_snapshot.jpg \
    --roi-x 0.30 --roi-y 0.55 --roi-width 0.25 --roi-height 0.25 \
    --save-roi /tmp/roi_preview.png
```

This prints the red-pixel percentage for that region and saves a cropped
`roi_preview.png` so you can confirm the box actually covers the bin's lid.
Adjust `--roi-*` until the crop lines up, and note the red percentage with
the bin present vs. absent to pick a sensible
`red_pixel_threshold_percent` (it defaults to `10`, i.e. detect once >10% of
the region is red).

`roi_x`/`roi_y` are the top-left corner as a fraction (0–1) of the image
width/height; `roi_width`/`roi_height` are the box size the same way — this
keeps the region correct regardless of the camera's actual resolution.

## 3. Wire up the weekly check

Add a `rest_command` to your `configuration.yaml`:

```yaml
rest_command:
  bin_scan:
    url: "http://<addon-hostname>:8099/scan"
    method: POST
```

The add-on's hostname is its slug with `-` in place of `_`, reachable from
the HA container network — typically `local-bin_scanner` or check
**Settings → Add-ons → Bin Scanner → Info** for the exact hostname shown
there.

Then add an automation with a weekly time trigger:

```yaml
automation:
  - alias: "Weekly bin check"
    trigger:
      - platform: time
        at: "18:00:00"
    condition:
      - condition: time
        weekday:
          - fri
    action:
      - action: rest_command.bin_scan
```

Adjust the day/time to whenever the bin should have been brought back in by.

## Testing without waiting a week

You can trigger a scan manually, and use `dry_run` to check detection
without sending a notification, e.g. from the **Terminal & SSH** add-on:

```bash
curl -X POST "http://<addon-hostname>:8099/scan?dry_run=true"
# {"detected": true, "red_pct": 14.32, "notified": false, "dry_run": true}
```

## Repository layout

- `repository.yaml` — makes this repo installable as an HA add-on repository
- `bin_scanner/` — the add-on itself (Dockerfile, `config.yaml`, Flask app)
- `bin_scanner/tests/` — unit tests for the detection logic (`pytest bin_scanner/tests`)
- `tools/calibrate.py` — standalone ROI/threshold calibration helper
