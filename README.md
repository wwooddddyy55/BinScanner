# BinScanner

A tiny Home Assistant add-on that checks a camera snapshot for a red-lidded
bin still sitting out front, and sends a push notification if it's still
there. Works in both daylight (color) and infrared/night mode, since it's
designed to be checked repeatedly through bin-night evening as it gets
later.

It's built for a low resource footprint: no ML model, no OpenCV/numpy — just
a Pillow color-threshold check over a small, configurable region of the
frame, triggered by Home Assistant's own scheduler rather than a container
that's constantly polling.

## How it works

```
HA automation (your own schedule, e.g. several checks through bin-night evening)
  → rest_command → POST http://<addon-hostname>:8099/scan
      bin_scanner add-on:
        1. Fetches a snapshot from your existing camera entity via HA's API
        2. Crops the configured region of interest (where the bin sits)
        3. Picks a detection mode from the snapshot itself:
           - Color mode (daylight): largest connected "red" blob (HSV
             threshold + shape check, not just a raw pixel count)
           - Infrared mode (night/IR illuminators on, image is monochrome):
             largest blob that changed vs. a stored "bin confirmed absent"
             reference photo, since there's no color left to threshold
        4. If that blob is big enough and roughly bin-shaped → calls your
           notify.* service
```

Both modes share the same blob analysis: they look for the single largest
connected region of "interesting" pixels (red, or changed-from-reference)
and check its size *and* bounding-box aspect ratio, which rejects things a
naive pixel count would misfire on — a wet-pavement reflection, a sliver of
a red car passing in the background, scattered red leaves, a stray light
change. See `min_aspect_ratio`/`max_aspect_ratio` below.

### Infrared/night mode

Since this is meant to nag you through the evening (many checks land after
dark, when the camera switches to IR illuminators and shoots monochrome),
night detection isn't an edge case here — it's the mode most scans will
actually run in. There's no color signal at all in a monochrome IR image,
so instead of a color threshold, night detection compares the region against
a **reference snapshot you capture once**, with the bin confirmed absent, at
night. Anything that changed enough (and is shaped like the bin) counts as
detected.

Mode is chosen automatically per snapshot (by checking the image's overall
color saturation, not the clock), so this keeps working correctly across
dusk transitions without you having to schedule around it.

**Fail-safe policy:** if it's infrared mode and no night reference has been
captured yet, the add-on defaults to `detected: true` (sends the
notification) rather than silently reporting "all clear". For a reminder
whose entire job is nagging you about the bin, one extra notification is a
far cheaper mistake than a silent miss.

**Which camera to use:** pick a view where the bin sits a reasonable
distance from the lens against a plain, static background (e.g. an overhead
garage/driveway camera looking down at its storage spot) rather than a
fisheye doorbell view where the bin sits right against the lens — the
distortion and edge-cropping on a close fisheye shot makes the region
unstable even after calibration.

The add-on authenticates to Home Assistant automatically via the
Supervisor-injected token (`homeassistant_api: true` in `config.yaml`) — no
long-lived access token to create or manage.

## 1. Install the add-on

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add `https://github.com/wwooddddyy55/binscanner` and refresh.
3. Find **Bin Scanner** in the store and install it.
4. In the add-on's **Configuration** tab, set at minimum:
   - `camera_entity`: the camera entity for whichever view you're using
     (e.g. `camera.front_east` for a garage/driveway camera) — check under
     **Settings → Devices & Services → Entities** for the exact entity ID,
     the default here is just a guess
   - `notify_service`: your phone's notify service *without* the `notify.`
     prefix, e.g. `mobile_app_coreys_iphone` (find it under
     **Developer Tools → Actions**, search "notify")
   - `roi_x`, `roi_y`, `roi_width`, `roi_height`: the region of the frame
     where the bin normally sits (see calibration below)
   - `min_aspect_ratio`, `max_aspect_ratio`: how tall vs. wide the detected
     blob's bounding box is allowed to be (height ÷ width); defaults of
     `0.3`–`4.0` are permissive, tighten them if you get false positives
     from an oddly-shaped object
   - `ir_saturation_threshold`, `ir_change_threshold_percent`,
     `ir_change_zscore`: control infrared/night detection (see below) —
     the defaults are a reasonable starting point, tune via `/calibrate`
5. Start the add-on and check its log for `Running on http://0.0.0.0:8099`.

## 2. Calibrate the detection region

### Option A: draw it in the browser (recommended)

With the add-on running, open:

```
http://<addon-hostname>:8099/calibrate
```

(use whatever host port you've mapped `8099/tcp` to, if you changed it in
the add-on's Network settings). This page:

- Shows the current camera snapshot — click **Refresh snapshot** to grab a
  new one.
- Lets you click-and-drag directly on the image to draw the region of
  interest.
- Shows a **Color / daylight mode** or **Infrared / night mode** badge based
  on the snapshot itself, and runs whichever detector matches — so you can
  calibrate both by simply visiting the page at different times of day.
- Runs the add-on's actual detection code against that exact snapshot and
  shows the live result — the largest blob's size, its bounding-box aspect
  ratio, and detected/not-detected — as you adjust the box, the color
  sliders, the aspect-ratio bounds, or (at night) the infrared controls. No
  guessing or redeploying.
- Prints all the resulting values (ROI, threshold, aspect-ratio bounds, and
  the infrared settings) for you to copy into the add-on's **Configuration**
  tab (this page doesn't save anything itself — Home Assistant owns the
  stored options).

Drag the box until it tightly covers the bin's lid, adjust the threshold %
and aspect-ratio bounds until "Bin detected" matches reality with the bin
present vs. absent, copy the values into Configuration, and restart the
add-on.

**Capturing the night reference:** since most checks here happen after dark,
this step matters — go out at night once with the bin confirmed **not** in
frame, open `/calibrate`, click **Refresh snapshot** (the mode badge should
read "Infrared / night mode"), then click **Capture current snapshot as
night reference** in the Night reference panel. Re-draw the box and check
"Bin detected" reads false with the bin away and true once you put it back,
adjusting `ir_change_threshold_percent`/`ir_change_zscore` as needed. If the
driveway's appearance drifts noticeably over time (dirt, leaves, seasons),
just re-capture the reference the same way.

### Option B: offline CLI tool

If you'd rather not expose the add-on to test with, or want to iterate on a
saved image locally, `tools/calibrate.py` does the same math without needing
Docker or Home Assistant reachable:

```bash
python3 -m pip install pillow

# Day/color mode
python3 tools/calibrate.py garage_snapshot.jpg \
    --roi-x 0.80 --roi-y 0.43 --roi-width 0.08 --roi-height 0.23 \
    --save-roi /tmp/roi_preview.png

# Night/infrared mode - pass a reference photo taken with the bin confirmed absent
python3 tools/calibrate.py garage_snapshot_night.jpg \
    --reference garage_empty_night.jpg \
    --roi-x 0.80 --roi-y 0.43 --roi-width 0.08 --roi-height 0.23
```

The tool auto-detects which mode a snapshot is in (by its overall color
saturation) and runs the matching detector, printing the total/blob
percentages, aspect ratio, and whether it would be detected. Use
`--min-aspect-ratio`/`--max-aspect-ratio` to try different shape bounds, and
`--ir-change-zscore` to tune night sensitivity.

`roi_x`/`roi_y` are the top-left corner as a fraction (0–1) of the image
width/height; `roi_width`/`roi_height` are the box size the same way — this
keeps the region correct regardless of the camera's actual resolution.

## 3. Wire up your own schedule

The add-on only exposes `POST /scan` — when and how often to call it (a
single weekly check, or several escalating checks through bin-night evening)
and what the notification says are entirely up to your own HA automations.
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
there. Call `rest_command.bin_scan` from as many time triggers as you like;
each call is independent, and once the bin's actually gone `detected` comes
back `false` so later checks that same night just stay quiet.

## Testing without waiting a week

You can trigger a scan manually, and use `dry_run` to check detection
without sending a notification, e.g. from the **Terminal & SSH** add-on:

```bash
curl -X POST "http://<addon-hostname>:8099/scan?dry_run=true"
# {"detected": true, "red_pct": 14.32, "mode": "infrared", "notified": false, "dry_run": true}
```

`mode` is `color`, `infrared`, or `infrared-no-reference` (the fail-safe
case — see the add-on log for a reminder to capture a night reference).

## Repository layout

- `repository.yaml` — makes this repo installable as an HA add-on repository
- `bin_scanner/` — the add-on itself (Dockerfile, `config.yaml`, Flask app)
- `bin_scanner/tests/` — unit + Flask endpoint tests (`pytest bin_scanner/tests`)
- `tools/calibrate.py` — standalone ROI/threshold calibration helper
