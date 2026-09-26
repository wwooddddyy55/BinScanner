#!/usr/bin/env python3
"""Standalone calibration helper for BinScanner.

Run this against a saved camera snapshot to find good `roi_*` values before
configuring the add-on - no Home Assistant or Docker required, just Pillow.

Day/color mode example:
    python3 tools/calibrate.py garage_snapshot.jpg \\
        --roi-x 0.30 --roi-y 0.55 --roi-width 0.25 --roi-height 0.25 \\
        --save-roi /tmp/roi_preview.png

Night/infrared mode example (pass a reference photo taken with the bin
confirmed absent, at night):
    python3 tools/calibrate.py garage_snapshot_night.jpg \\
        --reference garage_empty_night.jpg \\
        --roi-x 0.30 --roi-y 0.55 --roi-width 0.25 --roi-height 0.25
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Reuse the same detection logic the add-on runs, without needing Flask/requests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin_scanner" / "app"))

from detector import (  # noqa: E402
    HsvThresholds,
    Roi,
    analyze_change_blob,
    analyze_red_blob,
    average_saturation,
    crop_roi,
    is_infrared_mode,
)
from PIL import Image  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", help="Path to a saved camera snapshot (JPEG/PNG)")
    parser.add_argument(
        "--reference",
        metavar="PATH",
        help="Night reference image (bin confirmed absent) - if given, runs change-based "
        "(infrared/night) detection instead of color detection",
    )
    parser.add_argument("--roi-x", type=float, default=0.30)
    parser.add_argument("--roi-y", type=float, default=0.55)
    parser.add_argument("--roi-width", type=float, default=0.25)
    parser.add_argument("--roi-height", type=float, default=0.25)
    parser.add_argument("--hue-min", type=int, default=0)
    parser.add_argument("--hue-max", type=int, default=12)
    parser.add_argument("--hue-min2", type=int, default=245)
    parser.add_argument("--hue-max2", type=int, default=255)
    parser.add_argument("--min-saturation", type=int, default=90)
    parser.add_argument("--min-value", type=int, default=60)
    parser.add_argument("--threshold-percent", type=float, default=10.0)
    parser.add_argument(
        "--min-aspect-ratio", type=float, default=0.3, help="Minimum blob bounding-box height/width ratio"
    )
    parser.add_argument(
        "--max-aspect-ratio", type=float, default=4.0, help="Maximum blob bounding-box height/width ratio"
    )
    parser.add_argument(
        "--ir-change-zscore",
        type=float,
        default=1.0,
        help="Night mode only: how many std-devs of luminance change counts as 'different'",
    )
    parser.add_argument(
        "--ir-saturation-threshold",
        type=float,
        default=15.0,
        help="Average whole-image saturation (0-255) below which a snapshot is treated as infrared/night",
    )
    parser.add_argument(
        "--save-roi", metavar="PATH", help="Save a crop of just the ROI, to visually confirm placement"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    roi = Roi(x=args.roi_x, y=args.roi_y, width=args.roi_width, height=args.roi_height)

    image = Image.open(args.image)
    image.load()

    avg_sat = average_saturation(image)
    infrared = args.reference is not None or is_infrared_mode(image, args.ir_saturation_threshold)

    print(f"Image size:           {image.size[0]}x{image.size[1]}")
    print(f"ROI:                  x={roi.x} y={roi.y} width={roi.width} height={roi.height}")
    print(f"Average saturation:   {avg_sat:.1f} (infrared/night threshold: {args.ir_saturation_threshold})")
    print(f"Mode:                 {'infrared (night)' if infrared else 'color (day)'}")

    if infrared:
        if not args.reference:
            print("\nThis looks like a night/infrared shot, but no --reference was given.")
            print("Pass --reference <path to a night photo with the bin confirmed absent> to test detection.")
            return 1

        reference = Image.open(args.reference)
        reference.load()

        analysis = analyze_change_blob(image, reference, roi, args.ir_change_zscore)
        shape_ok = (
            analysis.aspect_ratio is not None
            and args.min_aspect_ratio <= analysis.aspect_ratio <= args.max_aspect_ratio
        )
        detected = analysis.blob_pct >= args.threshold_percent and shape_ok

        print(f"Total changed area:   {analysis.total_pct:.2f}%")
        print(f"Largest changed blob: {analysis.blob_pct:.2f}% ({analysis.blob_pixel_count} px)")
        print(f"Blob aspect ratio:    {analysis.aspect_ratio}")
        print(f"Change sensitivity:   {args.ir_change_zscore} std-devs")
    else:
        thresholds = HsvThresholds(
            hue_min=args.hue_min,
            hue_max=args.hue_max,
            hue_min2=args.hue_min2,
            hue_max2=args.hue_max2,
            min_saturation=args.min_saturation,
            min_value=args.min_value,
        )
        analysis = analyze_red_blob(image, roi, thresholds)
        shape_ok = (
            analysis.aspect_ratio is not None
            and args.min_aspect_ratio <= analysis.aspect_ratio <= args.max_aspect_ratio
        )
        detected = analysis.blob_pct >= args.threshold_percent and shape_ok

        print(f"Total red in ROI:     {analysis.total_pct:.2f}%")
        print(f"Largest blob:         {analysis.blob_pct:.2f}% ({analysis.blob_pixel_count} px)")
        print(f"Blob aspect ratio:    {analysis.aspect_ratio}")

    print(f"Threshold:            {args.threshold_percent}%")
    print(f"Aspect ratio bounds:  {args.min_aspect_ratio} - {args.max_aspect_ratio}")
    print(f"Bin detected:         {detected}")

    if args.save_roi:
        crop_roi(image.convert("RGB"), roi).save(args.save_roi)
        print(f"Saved ROI preview:    {args.save_roi}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
