#!/usr/bin/env python3
"""
Automated PNG sanity checks for rendered output -- a smoke test that a render didn't fail in a
recognizable way (near-black, blown-out, flat/uniform, blurred, top-clipped), never a quality
score (see the Tier-2 realism plan's verification section: these thresholds are deliberately
loose, and passing them says nothing about whether a render looks *good*).

Runs inside Blender: bpy.data.images.load() does robust PNG decoding (correct filter
reconstruction, gamma, etc. -- reimplementing that by hand in the driver process would be its own
bug farm), and Blender's own bundled numpy (present on both 4.2.0 as 1.24.3 and 5.2.0 as 2.3.4,
see compat_report.md) does the math. Zero new dependencies for the 55MB prod zip -- though this
script itself is never invoked in production, only by scripts/render_local.py.

Usage:
    blender --background --factory-startup --python scripts/image_checks.py -- rendered.png
    blender --background --factory-startup --python scripts/image_checks.py -- rendered.png --out report.json

Exit code is 0 if all checks passed, 1 otherwise (so render_local.py can use it as a pass/fail
gate directly).
"""
import json
import sys

try:
    import bpy
    import numpy as np
except ImportError:
    print("Run inside Blender: blender --background --python image_checks.py -- image.png", file=sys.stderr)
    sys.exit(1)


# Calibrated by actually running this module against 5 real render3d outputs recovered from the
# two-tenant AgentCore test (draft-quality, 1920x1080: a cyberpunk street, two castle-in-storm
# renders including the pre-fix near-black-adjacent one, a sunset skyscraper, a car showroom).
# Measured gradient_energy on all 5 real, non-blurred renders came in at 0.0016-0.0039 -- well
# below the plan's originally-assumed 0.004 floor -- so that threshold is set from the observed
# minimum (0.0016) backed off ~20%, not guessed. Every other threshold had comfortable headroom
# against real output already; see each comment for the measurement.
THRESHOLDS = {
    "min_mean_luma": 0.03,        # comfortably below all 5 real samples (0.25-0.68); a genuinely
                                   # near-black render (the pre-fix stormy castle) scores far under this
    "max_mean_luma": 0.97,        # blown-out/clipped-white failure mode
    "min_luma_variance": 0.0008,  # lowest real sample measured 0.0037 -- a flat/uniform-color
                                   # frame would score near 0
    "max_modal_share": 0.85,      # highest real sample measured 0.306 ("purple blur"/uniform-
                                   # denoise failure would dominate the frame far more than that
    "min_gradient_energy": 0.0012,# lowest real sample measured 0.0016 (~25% headroom) -- catches
                                   # blur specifically, since variance alone can be high with no edges
}

TOP_ROW_FRACTION = 0.02  # top 2% of rows


def load_rgb(path):
    """Load a PNG via Blender's own image loader and return an (H, W, 3) float32 array in
    [0, 1], top-down row order (Blender's .pixels are stored bottom-up)."""
    img = bpy.data.images.load(path)
    w, h = img.size
    channels = img.channels
    pixels = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, channels)
    rgb = pixels[::-1, :, :3]
    bpy.data.images.remove(img)
    return rgb


def luma(rgb):
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def check_image(path, thresholds=None):
    thresholds = {**THRESHOLDS, **(thresholds or {})}
    rgb = load_rgb(path)
    h, w, _ = rgb.shape
    y = luma(rgb)

    mean_luma = float(y.mean())
    luma_variance = float(y.var())

    # Modal-color-share: bucket each pixel to a coarse 4-bit-per-channel bin, find the largest
    # bucket's share of total pixels.
    bins = (np.clip(rgb, 0, 1) * 15).astype(np.uint8)
    keys = (bins[..., 0].astype(np.int32) << 8) | (bins[..., 1].astype(np.int32) << 4) | bins[..., 2].astype(np.int32)
    _, counts = np.unique(keys, return_counts=True)
    modal_share = float(counts.max() / keys.size)

    # Mean gradient magnitude of luma -- catches blur specifically (a blurred image can have
    # plenty of luma variance from a soft overall gradient, but very little edge energy).
    gy, gx = np.gradient(y)
    gradient_energy = float(np.sqrt(gx ** 2 + gy ** 2).mean())

    # Top-row occupancy: fraction of the top N rows whose pixels differ noticeably from a
    # background sample (the frame's corners) -- intended to catch a subject clipped through the
    # top of frame. Reported but NOT gated on `passed`: calibrating this against the 5 recovered
    # real renders produced two "high occupancy" readings (a busy multi-object cyberpunk street
    # at 1.0, a full-frame skyline at 0.94) that turned out to be legitimate compositions, not
    # bugs -- a scene that's deliberately framed close/full only "looks like" a clipped subject
    # to a occupancy-vs-corner heuristic. Left informational until there's an actual clipped-
    # camera fixture to calibrate a real threshold against (see fixtures/README.md).
    corner_sample = np.stack([rgb[0, 0], rgb[0, -1]]).mean(axis=0)
    n_top = max(1, int(h * TOP_ROW_FRACTION))
    top_rows = rgb[:n_top]
    diff = np.abs(top_rows - corner_sample).sum(axis=-1)
    top_row_occupancy = float((diff > 0.15).mean())

    checks = {
        "mean_luma": (mean_luma, thresholds["min_mean_luma"] <= mean_luma <= thresholds["max_mean_luma"], False),
        "luma_variance": (luma_variance, luma_variance >= thresholds["min_luma_variance"], False),
        "modal_color_share": (modal_share, modal_share <= thresholds["max_modal_share"], False),
        "gradient_energy": (gradient_energy, gradient_energy >= thresholds["min_gradient_energy"], False),
        "top_row_occupancy": (top_row_occupancy, True, True),  # informational=True, see comment above
    }
    passed = all(ok for _, ok, informational in checks.values() if not informational)
    return {
        "path": path,
        "resolution": [w, h],
        "passed": passed,
        "checks": {
            name: {"value": value, "passed": ok, "informational": informational}
            for name, (value, ok, informational) in checks.items()
        },
    }


def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    if not argv:
        print("Usage: blender --background --python image_checks.py -- image.png [--out report.json]", file=sys.stderr)
        sys.exit(1)
    image_path = argv[0]
    out_path = argv[argv.index("--out") + 1] if "--out" in argv else None

    result = check_image(image_path)
    text = json.dumps(result, indent=2)
    if out_path:
        with open(out_path, "w") as f:
            f.write(text)
    print(text)
    sys.exit(0 if result["passed"] else 1)


main()
