"""
The actual Blender scene-building logic for the render3d agent, as a real importable module
instead of a Python source string assembled via f-strings.

Two ways this file gets executed:

1. Standalone, for local testing with no agent involved at all:
     blender --background --factory-startup --python blender_runtime.py -- scene.json out.png

2. In production: agent.py's _build_blender_script() reads this file's own source text and
   prepends a `PAYLOAD = json.loads(<repr of json.dumps(...)>)` line, then writes the
   concatenation out as the script handed to `blender --background --python <script>`. The
   PAYLOAD line runs first, so by the time this module's own code executes, PAYLOAD already
   exists as a global -- see _main() at the bottom, which checks for it before falling back to
   argv parsing.

Why JSON-payload-then-repr instead of splicing scene values into an f-string (the old
_build_blender_script did this): `json.dumps(x)` alone is not valid Python source wherever it
contains `true`/`false`/`null` -- those are NameErrors as bare tokens, only valid as JSON. Taking
`repr()` of the *whole* json.dumps() string turns it into a single Python string literal, which
is simultaneously (a) always valid Python regardless of what's inside it -- quotes, newlines,
backslashes, unicode, anything -- because Python's own string-literal escaping handles all of
that for us, and (b) still exactly the JSON text `json.loads` expects once the string literal is
evaluated. This eliminates a whole bug class the old generator had case-by-case (a `"` in an
object name, or the string value `"true"`, both broke the generated script outright).

This module intentionally has zero third-party dependencies -- only stdlib, plus an *optional*
`bpy` (only present when actually running inside Blender). That's what makes normalize_scene()
below unit-testable from a plain `python3 -c "import blender_runtime"` with no Blender install
at all, and makes this file paste-into-Blender runnable for manual debugging.

Version-compat notes (prod is pinned to Blender 4.2.0; local dev may be on a newer LTS -- see
README.md): never branch on bpy.app.version. Blender's enum *introspection* is unreliable
headless (e.g. scene.view_settings.view_transform.enum_items returns something like ['NONE']
under `--background` even though direct assignment of 'Standard'/'AgX'/'Khronos PBR Neutral' all
succeed) -- so every version-sensitive property here is set via try/except assignment against an
ordered candidate list (_senum/_sock below), never by listing what's "available" first.
"""
import json
import math
import os
import re
import sys
import uuid

try:
    import bpy
    from mathutils import Vector
except ImportError:
    bpy = None
    Vector = None


# =============================================================================
# Compat helpers -- probe by assigning, never by listing enum_items (see module docstring)
# =============================================================================

def _sattr(obj, name, value):
    """Best-effort setattr. Returns True if it stuck, False if this Blender version doesn't
    have the attribute or rejects this value -- never raises."""
    try:
        setattr(obj, name, value)
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def _senum(obj, name, candidates):
    """Try each candidate value for an enum property in order; returns the one that stuck, or
    None if every candidate was rejected by this Blender version."""
    for value in candidates:
        if _sattr(obj, name, value):
            return value
    return None


def _node(node_tree, bl_idname):
    """Find a node by bl_idname (the stable internal type identifier), never by its display
    name -- `nodes["Background"]` breaks the instant Blender's UI language isn't English, and
    more importantly is just fragile: bl_idname ('ShaderNodeBackground') never changes, the
    display name a user sees can."""
    for n in node_tree.nodes:
        if n.bl_idname == bl_idname:
            return n
    return None


def _sock(node, candidates, value, is_color=False):
    """Best-effort set an input socket's default_value, trying each candidate socket name in
    order. Principled BSDF socket names move around between Blender versions (e.g. 'Specular'
    became 'Specular IOR Level'; 'Transmission' became 'Transmission Weight'; 'Emission' split
    into 'Emission Color' + 'Emission Strength') -- this is how callers stay version-agnostic
    without ever checking bpy.app.version. Returns the socket name that worked, or None if the
    node has none of the candidates (logged by the caller, never raised).
    """
    for name in candidates:
        socket = node.inputs.get(name)
        if socket is None:
            continue
        try:
            socket.default_value = (*value, 1.0) if is_color else value
            return name
        except (TypeError, ValueError):
            continue
    return None


# =============================================================================
# Scalar/vector coercion -- every one of these replaces a bug found in the original
# f-string generator's scattered, inconsistent .get() calls (see CURRENT_ISSUE.md /
# the Tier-2 plan's bug audit table for the full list).
# =============================================================================

def _vec3(val, default):
    """Coerce val into an exact 3-tuple of floats. Pads short sequences by repeating the last
    element, truncates long ones, broadcasts a bare scalar to all three components, and falls
    back to `default` for anything missing or non-numeric. Fixes the original generator's bare
    positional indexing (`location[0]`, `location[1]`, `location[2]`), which raised IndexError
    on any model-authored vector shorter than 3 elements."""
    if val is None:
        return tuple(float(x) for x in default)
    if isinstance(val, (int, float)):
        return (float(val), float(val), float(val))
    if not isinstance(val, (list, tuple)) or not val:
        return tuple(float(x) for x in default)
    nums = []
    for x in val:
        try:
            nums.append(float(x))
        except (TypeError, ValueError):
            nums.append(0.0)
    if len(nums) >= 3:
        return tuple(nums[:3])
    return tuple((nums + [nums[-1]] * 3)[:3])


def _clamp_float(val, default, lo, hi):
    """Coerce to float and clamp to [lo, hi], falling back to `default` on anything
    non-numeric (including NaN, which compares unequal to itself)."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return float(default)
    if f != f:  # NaN
        return float(default)
    return max(lo, min(hi, f))


def _bool(val, default=True):
    """Coerce common truthy/falsy JSON representations to bool. Fixes the original generator's
    bare f-string interpolation of a Python bool, which for a model-authored *string* value like
    "true" emitted the bare token `true` into the generated script -- a NameError, since `true`
    (lowercase) isn't a Python literal."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(val, (int, float)):
        return bool(val)
    return default


_SAFE_IDENT_RE = re.compile(r"[^A-Za-z0-9 _.\-]")


def _safe_ident(name, fallback):
    """Sanitize a model-controlled Blender datablock name (object/material/light). The
    JSON-payload handoff already makes a raw `"` in a name harmless to the *script* (see module
    docstring), but the name still becomes a real Blender datablock name, so keep it to a sane
    character set and Blender's own 63-char datablock name limit."""
    if not isinstance(name, str) or not name.strip():
        return fallback
    cleaned = _SAFE_IDENT_RE.sub("_", name.strip())[:63]
    return cleaned or fallback


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(name, fallback_prefix="render"):
    """Sanitize a model-controlled output filename. `os.path.basename` strips any directory
    components first -- this specifically closes a real path-traversal bug in the original code,
    which did `os.path.join(RENDER_OUTPUT_DIR, output_filename)` with a completely unsanitized,
    model-controlled output_filename. A prompt-injected filename like
    "../../../etc/cron.d/malicious" would have escaped RENDER_OUTPUT_DIR entirely -- a real
    arbitrary-file-write primitive on the render host, not a hypothetical one. After stripping
    the path, the remaining basename is whitelisted to a safe character set and guaranteed to
    end in .png (Blender silently appends .png to render.filepath if it's missing, which is a
    second bug this fixes: os.path.exists() on the un-normalized path returned False even after
    a successful render).
    """
    base = os.path.basename(str(name or "").strip())
    base = _SAFE_FILENAME_RE.sub("_", base)
    base = base.strip("._") or f"{fallback_prefix}_{uuid.uuid4().hex[:8]}"
    if not base.lower().endswith(".png"):
        base += ".png"
    return base


# =============================================================================
# Budget caps -- there was previously no ceiling at all on samples/resolution/object count,
# meaning a model (or a prompt-injected user request) could ask for e.g. samples: 100000 or
# 500 objects and the render host would simply try to do it, at real GPU-instance cost.
# =============================================================================

MAX_SAMPLES = 4096
MIN_RESOLUTION = 64
MAX_RESOLUTION = 4096
MAX_OBJECTS = 60
MAX_LIGHTS = 20

PRIMITIVES = {"CUBE", "PLANE", "SPHERE", "CYLINDER", "CONE"}
LIGHT_TYPES = {"POINT", "SUN", "SPOT", "AREA"}
# HDRI isn't implemented yet (arriving in Phase 8 of the Tier-2 realism plan) -- accepted here so
# a model that guesses ahead of the current SYSTEM_PROMPT doesn't hard-fail, but setup_world()
# below falls back to COLOR for it today.
ENVIRONMENT_TYPES = {"COLOR", "SKY", "HDRI"}

# Confirmed identical on both Blender 4.2.0 (prod-pinned) and 5.2.0 (local dev) via
# capability_probe.py -- see scripts/compat_report.md. Whitelisted here (not probed at
# normalize_scene time, since normalize_scene has no bpy) so a bogus model-supplied value falls
# back to the default instead of silently no-op'ing deep inside setup_color_management().
VIEW_TRANSFORMS = {"Standard", "AgX", "Khronos PBR Neutral", "Filmic", "Filmic Log", "False Color", "Raw"}
LOOKS = {
    "None", "Very Low Contrast", "Low Contrast", "Medium Low Contrast", "Medium Contrast",
    "Medium High Contrast", "High Contrast", "Very High Contrast",
}

# Sky presets: named knobs on ShaderNodeTexSky's sun_elevation/sun_rotation/*_density properties
# (all confirmed present on both target Blender versions -- the only divergence is dust_density
# (4.2) vs aerosol_density (5.x), handled in setup_world by setting both and letting whichever
# one the running version has stick). turbidity is set too, as a floor for the legacy
# PREETHAM/HOSEK_WILKIE sky_type fallback (never expected to fire on either target version, but
# free to set and harmless if the property doesn't apply to the active sky_type).
SKY_PRESETS = {
    "noon":        {"sun_elevation_deg": 65, "sun_rotation_deg": 0,   "air_density": 1.0, "dust_density": 1.0, "ozone_density": 1.0, "turbidity": 3.0, "strength": 1.0},
    "golden_hour": {"sun_elevation_deg": 8,  "sun_rotation_deg": 45,  "air_density": 2.0, "dust_density": 2.5, "ozone_density": 1.0, "turbidity": 5.0, "strength": 1.1},
    "sunset":      {"sun_elevation_deg": 6,  "sun_rotation_deg": 200, "air_density": 3.0, "dust_density": 4.0, "ozone_density": 1.2, "turbidity": 7.0, "strength": 1.3},
    "blue_hour":   {"sun_elevation_deg": -4, "sun_rotation_deg": 220, "air_density": 1.5, "dust_density": 1.5, "ozone_density": 1.5, "turbidity": 3.0, "strength": 0.5},
    # Found via testing (not guessed): the originally-authored values here (air_density 5.0,
    # dust_density 6.0, turbidity 8.0, strength 0.8) blew the render out to near-white on BOTH
    # target Blender versions -- confirmed via image_checks.py's luma_variance check catching it
    # on 5.2.0 and a manual look at the (technically-passing-by-a-hair) 4.2.0 render showing the
    # same problem. Sky haze density stacks with environment strength multiplicatively; these
    # values don't.
    "overcast":    {"sun_elevation_deg": 35, "sun_rotation_deg": 0,   "air_density": 2.2, "dust_density": 2.5, "ozone_density": 1.0, "turbidity": 4.0, "strength": 0.45},
    "dawn":        {"sun_elevation_deg": 4,  "sun_rotation_deg": 100, "air_density": 2.0, "dust_density": 2.0, "ozone_density": 1.3, "turbidity": 4.0, "strength": 0.9},
}

# Not real skies: ShaderNodeTexSky renders black with the sun below the horizon (or absent), so
# "night" and "flat studio fill" are deliberately implemented as a plain Background colour
# instead -- matching the Night/moody preset already in SYSTEM_PROMPT (a dark environment
# compensated by strong near-subject lights, not a dark *sky*).
FLAT_ENV_PRESETS = {
    "night_city": {"color": (0.02, 0.02, 0.05), "strength": 0.2},
    "studio": {"color": (0.5, 0.5, 0.55), "strength": 0.8},
}

# Fills scene.lighting when the model leaves it empty -- a lazily-authored scene (or a scene
# with only geometry) still gets competently lit instead of relying entirely on the environment.
# Each entry is a list of raw light dicts in the same shape a model would author, so they flow
# through the normal per-light normalization loop in normalize_scene() unchanged.
LIGHTING_RIGS = {
    "studio_3point": [
        {"type": "AREA", "name": "Key", "location": [4.0, -4.0, 5.0], "energy": 1200, "size": 4.0, "color": [1.0, 1.0, 1.0]},
        {"type": "AREA", "name": "Fill", "location": [-4.0, -2.0, 3.5], "energy": 400, "size": 5.0, "color": [0.9, 0.95, 1.0]},
        {"type": "AREA", "name": "Rim", "location": [0.0, 4.0, 4.0], "energy": 600, "size": 3.0, "color": [1.0, 1.0, 1.0]},
    ],
    "dramatic_rim": [
        {"type": "AREA", "name": "RimKey", "location": [-3.0, 4.0, 3.0], "energy": 2000, "size": 2.0, "color": [0.8, 0.85, 1.0]},
        {"type": "POINT", "name": "LowFill", "location": [2.0, -2.0, 1.0], "energy": 150, "color": [1.0, 0.7, 0.5]},
    ],
    "overcast_soft": [
        {"type": "AREA", "name": "SoftTop", "location": [0.0, 0.0, 8.0], "energy": 900, "size": 12.0, "color": [0.9, 0.92, 0.95]},
    ],
    "night_street": [
        {"type": "AREA", "name": "StreetGlow", "location": [2.0, -1.5, 3.0], "energy": 2200, "size": 2.0, "color": [1.0, 0.6, 0.3]},
        {"type": "POINT", "name": "Bounce", "location": [-2.0, 1.0, 1.0], "energy": 300, "color": [0.4, 0.5, 0.9]},
    ],
}

# Every material parameter the renderer understands, with the same neutral defaults the
# renderer used before Phase 5 (base_color grey, no metal/transmission/emission/coat). Presets
# below are sparse overrides of this dict, not full copies -- so adding a preset is "which 3-4
# things differ from neutral", not re-specifying all ten fields every time.
# Camera presets supply only a default *location* -- rotation is always computed via
# _look_rotation (below, in the bpy-only section) pointing at camera.look_at (default: world
# origin), unless the model gives an explicit camera.rotation. This retires the original three
# presets' hand-computed Euler angles in favor of one shared, verified-accurate code path
# (max 0.062 rad difference from the old hand-typed values for the same location -- see
# _look_rotation's docstring) instead of one hand-typed rotation per preset.
CAMERA_PRESETS = {
    "wide": (10.5, -9.0, 7.5),
    "standard": (7.0, -6.0, 5.0),
    "close": (4.2, -3.6, 3.0),
    "hero_low": (6.0, -5.0, 1.0),    # low angle looking up -- dramatic/imposing
    "aerial": (3.0, -3.0, 18.0),     # near-overhead -- skylines, layouts
    "product": (3.0, -2.6, 2.0),     # tight, centered -- single small subject
}

DEFAULT_MATERIAL_PARAMS = {
    "base_color": (0.8, 0.8, 0.8),
    "metallic": 0.0,
    "roughness": 0.5,
    "specular": 0.5,
    "ior": 1.45,
    "transmission": 0.0,
    "alpha": 1.0,
    "emission_color": (0.0, 0.0, 0.0),
    "emission_strength": 0.0,
    "coat_weight": 0.0,
}


def _material_preset(overrides, texture=None):
    params = {**DEFAULT_MATERIAL_PARAMS, **overrides}
    if texture:
        params["texture"] = texture
    return params


# A representative starter set (not an exhaustive catalog) -- adding one is exactly the pattern
# above: base params to override, optionally a "texture" key naming a TEXTURE_BUILDERS entry.
# window_glass is deliberately an *opaque dark reflector*, not transmissive glass -- correct for
# a building facade at any distance a camera in this renderer works at, and far cheaper to render
# (no transmission bounces) than actual glass, which Phase 7 adds for cases that need it.
MATERIAL_PRESETS = {
    "concrete": _material_preset({"base_color": (0.55, 0.54, 0.52), "roughness": 0.85}, texture="grungy_noise"),
    "weathered_concrete": _material_preset({"base_color": (0.42, 0.41, 0.38), "roughness": 0.9}, texture="grungy_noise"),
    "asphalt": _material_preset({"base_color": (0.08, 0.08, 0.09), "roughness": 0.75}, texture="grungy_noise"),
    "rust": _material_preset({"base_color": (0.35, 0.18, 0.09), "roughness": 0.7}, texture="grungy_noise"),
    "brick": _material_preset({"base_color": (0.55, 0.25, 0.18), "roughness": 0.8}, texture="brick"),
    "brushed_metal": _material_preset({"base_color": (0.7, 0.7, 0.72), "metallic": 1.0, "roughness": 0.35}, texture="brushed"),
    "marble": _material_preset({"base_color": (0.85, 0.83, 0.8), "roughness": 0.15, "specular": 0.6}, texture="marble"),
    "chrome": _material_preset({"base_color": (0.9, 0.9, 0.92), "metallic": 1.0, "roughness": 0.03}),
    "car_paint": _material_preset({"base_color": (0.6, 0.05, 0.05), "metallic": 0.8, "roughness": 0.12, "coat_weight": 1.0}),
    "glass": _material_preset({"base_color": (0.95, 0.97, 1.0), "roughness": 0.02, "transmission": 1.0}),
    "tinted_glass": _material_preset({"base_color": (0.55, 0.72, 0.68), "roughness": 0.03, "transmission": 0.9}),
    "frosted_glass": _material_preset({"base_color": (0.9, 0.92, 0.95), "roughness": 0.4, "transmission": 0.85}),
    "window_glass": _material_preset({"base_color": (0.08, 0.1, 0.12), "metallic": 0.3, "roughness": 0.05}),
    "water": _material_preset({"base_color": (0.05, 0.15, 0.2), "roughness": 0.02, "transmission": 0.95, "ior": 1.33}),
    "neon": _material_preset({"base_color": (1.0, 1.0, 1.0), "emission_color": (1.0, 0.2, 0.6), "emission_strength": 5.0}),
    "lit_window": _material_preset({"base_color": (0.9, 0.85, 0.7), "emission_color": (1.0, 0.9, 0.7), "emission_strength": 3.0}),
    "office_facade": _material_preset(
        {"base_color": (0.08, 0.1, 0.12), "metallic": 0.3, "roughness": 0.05},
        texture="office_facade",
    ),
}

# Extra tunables for office_facade, read from the model's material dict directly (not folded
# into MATERIAL_PRESETS -- these vary per-building, not per-preset). See
# _texture_office_facade's docstring for what each one drives.
FACADE_SPEC_DEFAULTS = {
    "pitch_h": 1.0,
    "pitch_v": 1.2,
    "lit_fraction": 0.35,
}

# Catalog name only, never a model-supplied URL: a URL would let a prompt-injected request make
# the render host fetch an arbitrary remote file (SSRF) or an arbitrarily large one (disk-fill
# DoS). Real files live in hdri_assets/ (CC0, Poly Haven) and are pre-staged to S3 at deploy time
# by Terraform (aws_s3_object.hdri) -- never fetched from a third party at render time, which
# keeps renders reproducible and off an external runtime dependency. agent.py's _ensure_hdri()
# downloads from S3 and verifies against `sha256` before use; on any failure it rewrites the
# scene to `fallback_sky_preset` instead of failing the render (see normalize_scene and
# setup_world's HDRI branch below) -- a missing/corrupt HDRI must never break a render.
HDRI_CATALOG = {
    "clear_sky": {"filename": "kloofendal_43d_clear_puresky.hdr", "sha256": "de7ba9d0b070470dbb70d0144294c8708068df353537b26ab59c394707e84377", "fallback_sky_preset": "noon"},
    "golden_sunset": {"filename": "industrial_sunset_puresky.hdr", "sha256": "ce8235e4b1b10b620120ceeb32eb3e80af15ea29b09a8625a4bdae647dff328d", "fallback_sky_preset": "golden_hour"},
    "overcast": {"filename": "overcast_soil_puresky.hdr", "sha256": "2dbbbbb1323a8e8989db2e8306bd13099b215539e5adba41b85738a250a7904e", "fallback_sky_preset": "overcast"},
    "night_sky": {"filename": "moonless_golf.hdr", "sha256": "4f597078024bd81429431e872d466d8808653ad62a8bc8c61d8052af7466c3aa", "fallback_sky_preset": "night_city"},
    "studio": {"filename": "studio_small_03.hdr", "sha256": "30933d55e45f0795daf49f3cbefbe0e5ebcb821ee04fb0a2818c02ffc3938817", "fallback_sky_preset": "studio"},
    "field": {"filename": "sunflowers_puresky.hdr", "sha256": "39a18be788fda30e1b1929d4ebd78b5da14433a6e2271eff1928a35e481c5111", "fallback_sky_preset": "noon"},
}


def normalize_scene(scene):
    """
    Pure function: raw, untrusted, model-authored scene dict -> normalized, fully-defaulted,
    budget-capped scene dict. No bpy dependency at all -- unit-testable with a plain
    `python3 -c "import blender_runtime; blender_runtime.normalize_scene(...)"`, no Blender
    install required.

    This is the single place all of the old generator's scattered, bug-prone .get()-with-
    silent-bad-defaults logic now lives, fully explicit and testable. It's idempotent (safe to
    call twice on its own output) and is called on both sides of the render: once by agent.py
    before generating the script (so render_scene can report resolution/samples/warnings from
    values it can trust, instead of the old `scene['render_settings']['resolution_x']`, which
    raised KeyError whenever the model omitted render_settings entirely), and once inside
    render() below (so this module is correct even when run standalone against a raw fixture,
    with no agent.py involved).

    Returns a dict with keys: camera, environment, lighting, objects, render_settings, and
    _warnings (a list of human-readable strings describing anything that was corrected,
    truncated, or fell back to a default -- surfaced back to the caller, never silently eaten).
    """
    if not isinstance(scene, dict):
        scene = {}
    warnings = []

    # --- camera ---
    camera_in = scene.get("camera") or {}
    preset_name = camera_in.get("preset")
    if preset_name is not None and preset_name not in CAMERA_PRESETS:
        warnings.append(f"camera.preset {preset_name!r} not recognized, ignoring")
        preset_name = None
    default_location = CAMERA_PRESETS[preset_name] if preset_name else (7.0, -6.0, 5.0)

    explicit_rotation = camera_in.get("rotation")
    if explicit_rotation is not None:
        # Explicit rotation (radians, matching the original schema -- NOT objects[].rotation's
        # degrees, an intentional inconsistency: camera rotation predates the degrees convention
        # and changing units on an existing, documented field would silently break anything
        # already relying on it) always wins over look_at.
        rotation = _vec3(explicit_rotation, (1.1, 0.0, 0.8))
        look_at = None
    else:
        # No explicit rotation: always compute from look_at (default: world origin) via
        # _look_rotation at render time -- this is also the new default behavior with NO camera
        # block at all, replacing the old hardcoded (1.1, 0.0, 0.8) fallback with the equivalent
        # computed rotation (verified within 0.062 rad of it for the preset locations above).
        rotation = None
        look_at = _vec3(camera_in.get("look_at"), (0.0, 0.0, 0.0))

    camera = {
        "location": _vec3(camera_in.get("location"), default_location),
        "rotation": rotation,  # None means "compute from look_at at render time"
        "look_at": look_at,
        "focal_length": _clamp_float(camera_in.get("focal_length"), 50.0, 1.0, 300.0),
        "sensor_width": _clamp_float(camera_in.get("sensor_width"), 36.0, 1.0, 200.0),
        "dof_enabled": _bool(camera_in.get("dof_enabled"), False),
        "f_stop": _clamp_float(camera_in.get("f_stop"), 2.8, 0.5, 32.0),
        # None means "auto-derive from the camera-to-look_at distance at render time" -- the
        # common case (focus on whatever the camera is actually pointed at) needs no numeric
        # guess. focus_object (look up another object's location by name) isn't implemented:
        # camera is normalized before the objects list exists in this function, and reordering
        # for one minor convenience field isn't worth the complexity here.
        "focus_distance": (
            _clamp_float(camera_in.get("focus_distance"), 5.0, 0.1, 100.0)
            if camera_in.get("focus_distance") is not None else None
        ),
    }

    # --- environment ---
    env_in = scene.get("environment") or {}
    env_type = str(env_in.get("type", "COLOR")).upper()
    if env_type not in ENVIRONMENT_TYPES:
        warnings.append(f"environment.type {env_type!r} not recognized, using COLOR")
        env_type = "COLOR"

    preset = env_in.get("preset")
    if env_type == "SKY":
        valid_presets = set(SKY_PRESETS) | set(FLAT_ENV_PRESETS)
        if preset is not None and preset not in valid_presets:
            warnings.append(f"environment.preset {preset!r} not recognized, using 'noon'")
            preset = None
    elif env_type == "HDRI":
        if preset not in HDRI_CATALOG:
            if preset is not None:
                warnings.append(f"environment.preset {preset!r} not a recognized HDRI name, falling back to SKY 'noon'")
            # Unknown/missing HDRI name -- rewrite to a real SKY preset now rather than carrying
            # an HDRI type nothing can resolve. This is the normalizer-side half of the "a
            # missing HDRI must never fail a render" guarantee; setup_world's own try/except
            # around bpy.data.images.load is the other half, for the case where the name IS
            # valid but agent.py's _ensure_hdri() couldn't actually fetch/verify the file.
            env_type = "SKY"
            preset = None
    else:
        preset = None  # presets only apply to SKY/HDRI; silently ignored on COLOR, not an error

    # SKY/HDRI default to strength 1.0, not COLOR's 0.3 -- the old flat-colour default of 0.3 is
    # a direct cause of dark/flat renders, but it's still the right conservative default for a
    # plain COLOR world (the original template's value), so it's kept there unchanged.
    default_strength = 1.0 if env_type in ("SKY", "HDRI") else 0.3
    environment = {
        "type": env_type,
        "preset": preset,
        "color": _vec3(env_in.get("color"), (0.05, 0.05, 0.08)),
        "strength": _clamp_float(env_in.get("strength"), default_strength, 0.0, 100.0),
        # Set by agent.py after a successful _ensure_hdri() fetch, never by the model -- not
        # part of the public schema. Passed through verbatim so blender_runtime.render() (which
        # re-normalizes internally, including when run standalone with no agent.py involved at
        # all) still has the resolved local path to load.
        "hdri_path": env_in.get("_hdri_local_path"),
    }

    # --- lighting ---
    lighting_in = scene.get("lighting") or []
    if not isinstance(lighting_in, list):
        lighting_in = []
    if not lighting_in:
        rig_name = scene.get("lighting_rig")
        if rig_name in LIGHTING_RIGS:
            lighting_in = LIGHTING_RIGS[rig_name]
        elif rig_name is not None:
            warnings.append(f"lighting_rig {rig_name!r} not recognized, no fallback lighting added")

    # SUN lights default to the active SKY preset's own sun angles (when there is one) so cast
    # shadows point roughly the same way the visible sun does, instead of every SUN light
    # defaulting to an unrelated fixed direction.
    sky_defaults = SKY_PRESETS.get(environment["preset"]) if environment["type"] == "SKY" else None
    default_elevation_deg = sky_defaults["sun_elevation_deg"] if sky_defaults else 45.0
    default_azimuth_deg = sky_defaults["sun_rotation_deg"] if sky_defaults else 0.0

    lights = []
    for i, light_in in enumerate(lighting_in):
        if len(lights) >= MAX_LIGHTS:
            warnings.append(f"lighting truncated to {MAX_LIGHTS} (budget cap)")
            break
        if not isinstance(light_in, dict):
            continue
        light_type = str(light_in.get("type", "POINT")).upper()
        if light_type not in LIGHT_TYPES:
            warnings.append(f"lighting[{i}].type {light_type!r} not recognized, using POINT")
            light_type = "POINT"
        light = {
            "name": _safe_ident(light_in.get("name"), f"Light_{i}"),
            "type": light_type,
            "location": _vec3(light_in.get("location"), (0.0, 0.0, 5.0)),
            "energy": _clamp_float(light_in.get("energy"), 500.0, 0.0, 1_000_000.0),
            "color": _vec3(light_in.get("color"), (1.0, 1.0, 1.0)),
            "size": _clamp_float(light_in.get("size"), 3.0, 0.0, 1000.0),
        }
        if light_type in ("AREA", "SPOT"):
            # Previously every light pointed straight down (Blender's default object rotation),
            # regardless of where it was placed -- an offset key/fill/rim light (the normal case)
            # never actually illuminated the subject it was positioned near. aim_at defaults to
            # the origin, matching the existing "keep geometry centered at the origin" convention
            # camera presets already rely on.
            light["aim_at"] = _vec3(light_in.get("aim_at"), (0.0, 0.0, 0.0))
        if light_type == "SPOT":
            light["spot_size_deg"] = _clamp_float(light_in.get("spot_size_deg"), 45.0, 1.0, 170.0)
            light["spot_blend"] = _clamp_float(light_in.get("spot_blend"), 0.15, 0.0, 1.0)
        if light_type == "SUN":
            light["elevation_deg"] = _clamp_float(light_in.get("elevation_deg"), default_elevation_deg, -90.0, 90.0)
            light["azimuth_deg"] = _clamp_float(light_in.get("azimuth_deg"), default_azimuth_deg, 0.0, 360.0)
        lights.append(light)

    # --- objects ---
    objects_in = scene.get("objects") or []
    if not isinstance(objects_in, list):
        objects_in = []
    if len(objects_in) > MAX_OBJECTS:
        warnings.append(f"objects truncated to {MAX_OBJECTS} (budget cap)")
    objects = []
    for i, obj_in in enumerate(objects_in[:MAX_OBJECTS]):
        if not isinstance(obj_in, dict):
            continue
        primitive = str(obj_in.get("primitive", "CUBE")).upper()
        if primitive not in PRIMITIVES:
            warnings.append(f"objects[{i}].primitive {primitive!r} not recognized, using CUBE")
            primitive = "CUBE"
        mat_in = obj_in.get("material") or {}
        # material.preset supplies defaults for every param below (sparse overrides of
        # DEFAULT_MATERIAL_PARAMS -- see MATERIAL_PRESETS); any field the model *also* sets
        # explicitly in this material dict still wins over the preset, same override precedence
        # camera/environment presets already use. An unrecognized preset falls back to the
        # original flat neutral defaults rather than erroring.
        preset_name = mat_in.get("preset")
        if preset_name is not None and preset_name not in MATERIAL_PRESETS:
            warnings.append(f"objects[{i}].material.preset {preset_name!r} not recognized, ignoring")
            preset_name = None
        base_params = MATERIAL_PRESETS[preset_name] if preset_name else DEFAULT_MATERIAL_PARAMS
        material = {
            "name": _safe_ident(mat_in.get("name"), f"Material_{i}"),
            "preset": preset_name,
            "texture": base_params.get("texture"),
            "base_color": _vec3(mat_in.get("base_color"), base_params["base_color"]),
            "metallic": _clamp_float(mat_in.get("metallic"), base_params["metallic"], 0.0, 1.0),
            "roughness": _clamp_float(mat_in.get("roughness"), base_params["roughness"], 0.0, 1.0),
            # Revived dead key: the schema has always documented "specular" but the renderer
            # never read it until Phase 0. Wired to the Principled BSDF's "Specular IOR Level"
            # socket in setup_objects() below.
            "specular": _clamp_float(mat_in.get("specular"), base_params["specular"], 0.0, 1.0),
            "ior": _clamp_float(mat_in.get("ior"), base_params["ior"], 1.0, 3.0),
            "transmission": _clamp_float(mat_in.get("transmission"), base_params["transmission"], 0.0, 1.0),
            "alpha": _clamp_float(mat_in.get("alpha"), base_params["alpha"], 0.0, 1.0),
            "emission_color": _vec3(mat_in.get("emission_color"), base_params["emission_color"]),
            "emission_strength": _clamp_float(mat_in.get("emission_strength"), base_params["emission_strength"], 0.0, 100.0),
            "coat_weight": _clamp_float(mat_in.get("coat_weight"), base_params["coat_weight"], 0.0, 1.0),
        }
        if material["texture"] == "office_facade":
            # facade_spec fields vary per-building (a model might want tighter window spacing or
            # more lit windows for a busy office tower vs a quiet residential one), so they're
            # read directly from the material dict rather than folded into MATERIAL_PRESETS.
            material["facade_spec"] = {
                "pitch_h": _clamp_float(mat_in.get("pitch_h"), FACADE_SPEC_DEFAULTS["pitch_h"], 0.3, 5.0),
                "pitch_v": _clamp_float(mat_in.get("pitch_v"), FACADE_SPEC_DEFAULTS["pitch_v"], 0.3, 5.0),
                "lit_fraction": _clamp_float(mat_in.get("lit_fraction"), FACADE_SPEC_DEFAULTS["lit_fraction"], 0.0, 1.0),
            }
        # rotation is authored in degrees (rotation_radians is an escape hatch for callers that
        # already have radians) -- degrees match how a model naturally reasons about "tilt this
        # 15 degrees", the same way camera presets exist so nobody hand-computes Euler radians.
        if obj_in.get("rotation_radians") is not None:
            rotation = _vec3(obj_in.get("rotation_radians"), (0.0, 0.0, 0.0))
        else:
            rotation_deg = _vec3(obj_in.get("rotation"), (0.0, 0.0, 0.0))
            rotation = tuple(math.radians(d) for d in rotation_deg)

        objects.append({
            "name": _safe_ident(obj_in.get("name"), f"Object_{i}"),
            "primitive": primitive,
            "location": _vec3(obj_in.get("location"), (0.0, 0.0, 0.0)),
            "scale": _vec3(obj_in.get("scale"), (1.0, 1.0, 1.0)),
            "rotation": rotation,
            "material": material,
        })

    # --- render settings ---
    rs_in = scene.get("render_settings") or {}
    view_transform = rs_in.get("view_transform")
    if view_transform not in VIEW_TRANSFORMS:
        if view_transform is not None:
            warnings.append(f"render_settings.view_transform {view_transform!r} not recognized, using 'Khronos PBR Neutral'")
        # Khronos PBR Neutral, not Blender's own default of AgX: AgX deliberately desaturates and
        # flattens for a filmic look, which is a direct, measured contributor to the washed-out
        # renders this phase exists to fix. Khronos PBR Neutral still rolls off blown highlights
        # (so a bright sun doesn't clip to white paste) while preserving albedo/saturation.
        view_transform = "Khronos PBR Neutral"
    look = rs_in.get("look")
    if look not in LOOKS:
        if look is not None:
            warnings.append(f"render_settings.look {look!r} not recognized, ignoring")
        look = None

    # Auto-escalated (not model-controlled): real transmissive glass needs far more light-path
    # bounces than Blender's own defaults (12) allow, or stacked/layered glass goes black past
    # the 12th surface -- the classic "why is my glass building opaque" failure. Detected from
    # the objects the model actually authored, not a flag it has to remember to set.
    has_transmission = any(obj["material"]["transmission"] > 0.0 for obj in objects)
    glass_quality = rs_in.get("glass_quality") if rs_in.get("glass_quality") in ("standard", "high") else "standard"

    render_settings = {
        "samples": int(_clamp_float(rs_in.get("samples"), 256, 1, MAX_SAMPLES)),
        "resolution_x": int(_clamp_float(rs_in.get("resolution_x"), 1920, MIN_RESOLUTION, MAX_RESOLUTION)),
        "resolution_y": int(_clamp_float(rs_in.get("resolution_y"), 1080, MIN_RESOLUTION, MAX_RESOLUTION)),
        "use_denoising": _bool(rs_in.get("use_denoising"), True),
        "view_transform": view_transform,
        "exposure": _clamp_float(rs_in.get("exposure"), 0.0, -10.0, 10.0),
        "look": look,
        "has_transmission": has_transmission,
        "glass_quality": glass_quality,
    }

    return {
        "camera": camera,
        "environment": environment,
        "lighting": lights,
        "objects": objects,
        "render_settings": render_settings,
        "_warnings": warnings,
    }


# =============================================================================
# Blender-side builders (bpy required past this point)
# =============================================================================

_PRIMITIVE_OPS = {
    "CUBE": "primitive_cube_add",
    "PLANE": "primitive_plane_add",
    "SPHERE": "primitive_uv_sphere_add",
    "CYLINDER": "primitive_cylinder_add",
    "CONE": "primitive_cone_add",
}

# (candidate socket names, is_color) -- see _sock's docstring for why these are candidate lists.
_MATERIAL_SOCKETS = {
    "base_color": (["Base Color"], True),
    "metallic": (["Metallic"], False),
    "roughness": (["Roughness"], False),
    "specular": (["Specular IOR Level", "Specular"], False),
    "ior": (["IOR"], False),
    "transmission": (["Transmission Weight", "Transmission"], False),
    "alpha": (["Alpha"], False),
    "emission_color": (["Emission Color", "Emission"], True),
    "emission_strength": (["Emission Strength"], False),
    "coat_weight": (["Coat Weight", "Clearcoat"], False),
}


def setup_color_management(bl_scene, render_settings):
    """Set view_transform (default Khronos PBR Neutral, not Blender's own AgX default -- see
    normalize_scene's comment), exposure, and best-effort look. look is set *after*
    view_transform deliberately: valid look identifiers are scoped to the active view transform
    (e.g. AgX only accepts "None"), so setting it first against whatever the previous
    view_transform was would be meaningless."""
    vs = bl_scene.view_settings
    _senum(vs, "view_transform", [render_settings["view_transform"], "Khronos PBR Neutral", "Standard"])
    vs.exposure = render_settings["exposure"]
    if render_settings.get("look"):
        _sattr(vs, "look", render_settings["look"])


def setup_render_settings(bl_scene, render_settings):
    bl_scene.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences

    gpu_available = False
    device_type_used = None
    for device_type in ["OPTIX", "METAL", "HIP", "CUDA"]:
        if not _sattr(prefs, "compute_device_type", device_type):
            continue
        try:
            prefs.get_devices()
        except Exception:
            continue
        gpu_devices = [d for d in prefs.devices if d.type != "CPU"]
        if gpu_devices:
            for device in prefs.devices:
                device.use = (device.type != "CPU")
            bl_scene.cycles.device = "GPU"
            gpu_available = True
            device_type_used = device_type
            break

    if not gpu_available:
        bl_scene.cycles.device = "CPU"

    # A machine-parseable marker line, instead of agent.py checking "GPU" in result.stdout --
    # that check was *always* true, because the CPU-fallback path's own log message contains the
    # substring "GPU" ("GPU not available, using CPU rendering"), so every CPU render was
    # unconditionally misreported as a GPU render. See agent.py's _parse_render_device().
    print("RENDER_DEVICE " + json.dumps({"gpu": gpu_available, "device_type": device_type_used}))

    bl_scene.cycles.samples = render_settings["samples"]
    bl_scene.cycles.use_denoising = render_settings["use_denoising"]
    if gpu_available and device_type_used == "OPTIX":
        _senum(bl_scene.cycles, "denoiser", ["OPTIX", "OPENIMAGEDENOISE"])
    else:
        _senum(bl_scene.cycles, "denoiser", ["OPENIMAGEDENOISE"])

    bl_scene.render.resolution_x = render_settings["resolution_x"]
    bl_scene.render.resolution_y = render_settings["resolution_y"]
    bl_scene.render.resolution_percentage = 100
    bl_scene.render.image_settings.file_format = "PNG"

    if render_settings["has_transmission"]:
        # Real transmissive glass needs far more light-path bounces than Blender's own defaults
        # (12) -- without this, light passing through several glass surfaces in a row (a window
        # pane plus whatever's behind it, or two panes of a glass box) runs out of bounces and
        # renders black past the 12th surface, the classic "why is my glass building opaque"
        # failure. Auto-escalated from normalize_scene's has_transmission detection, not
        # model-controlled -- there's no reason a model authoring a glass object would think to
        # ask for this.
        _sattr(bl_scene.cycles, "max_bounces", 24)
        _sattr(bl_scene.cycles, "transmission_bounces", 24)
        _sattr(bl_scene.cycles, "transparent_max_bounces", 24)
        _sattr(bl_scene.cycles, "blur_glossy", 1.0)  # kills fireflies from sharp glass caustics
        if render_settings["glass_quality"] == "high":
            _sattr(bl_scene.cycles, "caustics_refractive", True)
            # Transmission is noisier than diffuse -- give it more samples to clean up,
            # capped at the same budget ceiling as everything else.
            bl_scene.cycles.samples = min(MAX_SAMPLES, int(render_settings["samples"] * 1.5))


def setup_world(bl_scene, environment):
    world = bpy.data.worlds.new("World")
    bl_scene.world = world
    world.use_nodes = True
    node_tree = world.node_tree
    bg = _node(node_tree, "ShaderNodeBackground")

    env_type = environment["type"]

    if env_type == "HDRI":
        # normalize_scene already validated the catalog name -- reaching here with type still
        # "HDRI" means the name IS a real HDRI_CATALOG entry. What's NOT guaranteed is that the
        # file actually exists on disk: hdri_path is only set (by agent.py, after a successful
        # agent-side _ensure_hdri() S3 fetch+sha256 verify) when that fetch succeeded, and even
        # a present file can fail to load (corrupt download, unsupported format on this Blender
        # build). Both cases fall through to the catalog entry's fallback_sky_preset rather than
        # failing the render -- "a missing HDRI must never fail a render" is a hard requirement,
        # not a nice-to-have, since this runs on a live GPU-cost render request.
        catalog_entry = HDRI_CATALOG.get(environment["preset"], {})
        hdri_path = environment.get("hdri_path")
        loaded = False
        if hdri_path:
            try:
                image = bpy.data.images.load(hdri_path)
                env_tex = node_tree.nodes.new("ShaderNodeTexEnvironment")
                env_tex.image = image
                node_tree.links.new(env_tex.outputs["Color"], bg.inputs["Color"])
                _sock(bg, ["Strength"], environment["strength"])
                loaded = True
            except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring above
                print(f"Failed to load HDRI at {hdri_path!r}: {exc} -- falling back to SKY")
        if loaded:
            return
        fallback_preset = catalog_entry.get("fallback_sky_preset", "noon")
        print(f"HDRI {environment['preset']!r} unavailable, falling back to SKY preset {fallback_preset!r}")
        environment = {**environment, "type": "SKY", "preset": fallback_preset}
        env_type = "SKY"
        # falls through to the SKY branch below, not a return -- same object, just a different path

    if env_type == "SKY" and environment["preset"] in FLAT_ENV_PRESETS:
        # Not a real sky -- see FLAT_ENV_PRESETS' docstring comment.
        flat = FLAT_ENV_PRESETS[environment["preset"]]
        _sock(bg, ["Color"], flat["color"], is_color=True)
        _sock(bg, ["Strength"], flat["strength"])
        return

    if env_type == "SKY":
        preset = SKY_PRESETS[environment["preset"]] if environment["preset"] in SKY_PRESETS else SKY_PRESETS["noon"]
        sky = node_tree.nodes.new("ShaderNodeTexSky")
        _senum(sky, "sky_type", ["MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "NISHITA", "PREETHAM", "HOSEK_WILKIE"])
        _sattr(sky, "sun_elevation", math.radians(preset["sun_elevation_deg"]))
        _sattr(sky, "sun_rotation", math.radians(preset["sun_rotation_deg"]))
        _sattr(sky, "air_density", preset["air_density"])
        _sattr(sky, "dust_density", preset["dust_density"])      # Blender 4.2 property name
        _sattr(sky, "aerosol_density", preset["dust_density"])   # renamed in Blender 5.x
        _sattr(sky, "ozone_density", preset["ozone_density"])
        _sattr(sky, "turbidity", preset["turbidity"])            # legacy PREETHAM/HOSEK_WILKIE floor only
        # The codebase's first-ever links.new() call -- everything before this phase only ever
        # set a node's own input default_value, never wired one node's output into another's
        # input.
        node_tree.links.new(sky.outputs["Color"], bg.inputs["Color"])
        _sock(bg, ["Strength"], environment["strength"])
        return

    # COLOR (default / fallback)
    _sock(bg, ["Color"], environment["color"], is_color=True)
    _sock(bg, ["Strength"], environment["strength"])


def _look_rotation(from_loc, to_loc):
    """Compute an Euler rotation that points an object's local -Z axis (Blender's default
    'forward' for lights and cameras) from from_loc toward to_loc. Verified against the existing
    camera presets (max 0.062 rad difference from their hand-computed Euler angles for the same
    location-looking-at-origin geometry) -- accurate enough to trust for lights, though the
    camera presets themselves stay hand-authored for now (revisited in Phase 9)."""
    direction = Vector(to_loc) - Vector(from_loc)
    if direction.length < 1e-9:
        return (0.0, 0.0, 0.0)
    return tuple(direction.to_track_quat('-Z', 'Y').to_euler())


def _sun_direction_rotation(elevation_deg, azimuth_deg):
    """Point a SUN lamp's local -Z axis in the direction sunlight actually travels (from the sun
    position down toward the scene), using the same elevation/azimuth convention SKY_PRESETS
    uses for ShaderNodeTexSky's sun_elevation/sun_rotation. This makes a SUN light that inherits
    a sky preset's angles (see normalize_scene) cast shadows in a direction consistent with the
    preset's mood (e.g. long low-angle shadows for 'sunset') -- Blender's exact internal sky-disc
    azimuth convention isn't independently verified here, so treat this as "a consistent,
    physically-plausible direction", not "pixel-aligned with the rendered sun disc"."""
    elevation = math.radians(elevation_deg)
    azimuth = math.radians(azimuth_deg)
    sun_point = Vector((
        math.cos(elevation) * math.cos(azimuth),
        math.cos(elevation) * math.sin(azimuth),
        math.sin(elevation),
    ))
    return _look_rotation(sun_point, Vector((0.0, 0.0, 0.0)))


def setup_camera(bl_scene, camera):
    cam_data = bpy.data.cameras.new("Camera")
    cam_data.lens = camera["focal_length"]
    cam_data.sensor_width = camera["sensor_width"]
    cam_obj = bpy.data.objects.new("Camera", cam_data)
    bl_scene.collection.objects.link(cam_obj)
    cam_obj.location = camera["location"]

    if camera["rotation"] is not None:
        cam_obj.rotation_euler = camera["rotation"]
        look_at_point = None
    else:
        look_at_point = camera["look_at"]
        cam_obj.rotation_euler = _look_rotation(camera["location"], look_at_point)

    bl_scene.camera = cam_obj

    if camera["dof_enabled"]:
        cam_data.dof.use_dof = True
        _sattr(cam_data.dof, "aperture_fstop", camera["f_stop"])
        focus_distance = camera["focus_distance"]
        if focus_distance is None:
            target = look_at_point if look_at_point is not None else (0.0, 0.0, 0.0)
            focus_distance = (Vector(camera["location"]) - Vector(target)).length
        cam_data.dof.focus_distance = focus_distance


def setup_lights(bl_scene, lights):
    for light in lights:
        light_data = bpy.data.lights.new(name=light["name"], type=light["type"])
        light_data.energy = light["energy"]
        light_data.color = light["color"]
        # hasattr, not an exact type-string match -- the original generator only set `size` when
        # `light.get("type") == "AREA"` with exact-case match, which both silently dropped size
        # on a model-authored lowercase "area" (now normalized upstream in normalize_scene
        # anyway) and would have KeyError'd if AREA ever stopped being the only type with size.
        if hasattr(light_data, "size"):
            light_data.size = light["size"]
        if light["type"] == "SPOT":
            _sattr(light_data, "spot_size", math.radians(light["spot_size_deg"]))
            _sattr(light_data, "spot_blend", light["spot_blend"])

        light_obj = bpy.data.objects.new(light["name"], light_data)
        bl_scene.collection.objects.link(light_obj)
        light_obj.location = light["location"]

        # Previously every light kept Blender's default rotation (identity -- local -Z straight
        # down), regardless of type or position: an AREA/SPOT light offset to the side (the
        # normal case for a key/fill/rim setup) never actually pointed at the subject it was
        # placed near. POINT is genuinely omnidirectional -- no rotation needed or meaningful.
        if light["type"] in ("AREA", "SPOT"):
            light_obj.rotation_euler = _look_rotation(light["location"], light["aim_at"])
        elif light["type"] == "SUN":
            light_obj.rotation_euler = _sun_direction_rotation(light["elevation_deg"], light["azimuth_deg"])


# Tessellation args per primitive, applied at creation time -- Blender's own defaults (32
# segments/16 rings for a UV sphere, 32 vertices for cylinder/cone) look faceted once an object
# fills a meaningful part of the frame; bumping this once here costs nothing at render time but
# fixes visibly faceted spheres in every scene, not just ones that happen to need it.
_TESSELLATION_KWARGS = {
    "SPHERE": {"segments": 48, "ring_count": 24},
    "CYLINDER": {"vertices": 48},
    "CONE": {"vertices": 48},
}

# Auto-bevel only box-ish primitives -- a bevel on an already-curved SPHERE/CYLINDER/CONE cap
# would be visually redundant (see shade_smooth_by_angle below for how those get their rounded
# look instead). PLANE is excluded too: a bevel on a paper-thin ground plane's edge is never
# visible at the camera distances this renderer works at.
_BEVEL_PRIMITIVES = {"CUBE"}
_BEVEL_WIDTH_MAX = 0.06
_BEVEL_WIDTH_DIMENSION_FRACTION = 0.15  # clamp so bevel width can't exceed 15% of the smallest
                                          # dimension -- prevents self-intersection on thin slabs
_BEVEL_SEGMENTS = 2
_BEVEL_ANGLE_LIMIT_DEG = 30
_SHADE_SMOOTH_ANGLE_DEG = 30


def _mix_color(node_tree, fac_socket, color1, color2):
    """Create a MixRGB node and wire it up. Candidate-list the Fac socket name ('Fac' on 4.2,
    'Factor' on 5.x -- found via capability_probe.py, not in the original compat allowlist) and
    return the node so callers can link its Color output onward."""
    mix = node_tree.nodes.new("ShaderNodeMixRGB")
    fac_input = mix.inputs.get("Factor") or mix.inputs.get("Fac")
    node_tree.links.new(fac_socket, fac_input)
    mix.inputs["Color1"].default_value = color1
    mix.inputs["Color2"].default_value = color2
    return mix


def _add_color_variation(node_tree, base_color_output):
    """Multiply a color node's output by a small per-object-instance random scalar (derived from
    ShaderNodeObjectInfo's Random output, which is stable per object across renders) so several
    objects sharing one textured material preset -- e.g. ten office-tower cubes all using
    'concrete' -- don't render as ten literally identical color swatches. Returns the varied
    output socket. Scoped to textured presets only (see TEXTURE_BUILDERS) rather than every
    material: a flat, untextured color would need an extra node just to have something to vary,
    for a much subtler payoff than varying an already-noisy texture.
    """
    obj_info = node_tree.nodes.new("ShaderNodeObjectInfo")
    map_range = node_tree.nodes.new("ShaderNodeMapRange")
    _sattr(map_range, "clamp", True)
    _sock(map_range, ["To Min"], 0.85)
    _sock(map_range, ["To Max"], 1.15)
    node_tree.links.new(obj_info.outputs["Random"], map_range.inputs["Value"])

    combine = node_tree.nodes.new("ShaderNodeCombineXYZ")
    for axis in ("X", "Y", "Z"):
        node_tree.links.new(map_range.outputs["Result"], combine.inputs[axis])

    vary = node_tree.nodes.new("ShaderNodeVectorMath")
    _sattr(vary, "operation", "MULTIPLY")
    node_tree.links.new(base_color_output, vary.inputs[0])
    node_tree.links.new(combine.outputs["Vector"], vary.inputs[1])
    return vary.outputs["Vector"]


def _texture_grungy_noise(node_tree, bsdf, spec):
    """Generic weathered-surface texture: one noise field colour-ramped against the preset's own
    base color for patchy tonal variation (concrete/asphalt/rust/weathered_concrete all share
    this -- they differ only in base_color/roughness, not in *pattern*), the same noise driving a
    Bump for surface micro-detail, and a second, differently-scaled noise driving Roughness so
    the surface isn't uniformly glossy or matte. Only TexNoise/Bump sockets confirmed present on
    both target Blender versions are used (Scale/Detail/Roughness; Strength/Distance/Height) --
    see scripts/compat_report.md."""
    base_rgba = tuple(bsdf.inputs["Base Color"].default_value)

    noise1 = node_tree.nodes.new("ShaderNodeTexNoise")
    _sock(noise1, ["Scale"], 8.0)
    _sock(noise1, ["Detail"], 6.0)
    _sock(noise1, ["Roughness"], 0.6)

    darker = tuple(c * 0.7 for c in base_rgba[:3]) + (1.0,)
    mix = _mix_color(node_tree, noise1.outputs["Fac"], darker, base_rgba)
    varied = _add_color_variation(node_tree, mix.outputs["Color"])
    node_tree.links.new(varied, bsdf.inputs["Base Color"])

    bump = node_tree.nodes.new("ShaderNodeBump")
    _sock(bump, ["Strength"], 0.15)
    _sock(bump, ["Distance"], 0.3)
    node_tree.links.new(noise1.outputs["Fac"], bump.inputs["Height"])
    node_tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])

    noise2 = node_tree.nodes.new("ShaderNodeTexNoise")
    _sock(noise2, ["Scale"], 2.0)
    _sock(noise2, ["Detail"], 4.0)
    base_rough = bsdf.inputs["Roughness"].default_value
    rough_ramp = node_tree.nodes.new("ShaderNodeMapRange")
    _sattr(rough_ramp, "clamp", True)
    _sock(rough_ramp, ["To Min"], max(0.0, base_rough - 0.15))
    _sock(rough_ramp, ["To Max"], min(1.0, base_rough + 0.15))
    node_tree.links.new(noise2.outputs["Fac"], rough_ramp.inputs["Value"])
    node_tree.links.new(rough_ramp.outputs["Result"], bsdf.inputs["Roughness"])


def _wall_uv(node_tree):
    """Synthetic 2D wall coordinate: X+Y as the horizontal axis (so a +Y-facing wall varies with
    X and a +X-facing wall varies with Y -- one of the two is always ~0 for an axis-aligned box
    face) and Z as the vertical axis. Fixes a real, confirmed bug: TexBrick (and any other
    2D-repeating pattern) patterns its Vector input's raw X and Y components, so feeding it the
    default Object coordinate directly renders as vertical *stripes* on a wall, not a grid --
    because a vertical wall's "up" axis is world Z, not object-space Y. Correct for the common
    case (axis-aligned vertical walls) both _texture_brick and the office_facade window-grid
    builder below are meant for; not exact for a rotated wall or a top/bottom face.
    """
    coord = node_tree.nodes.new("ShaderNodeTexCoord")
    separate = node_tree.nodes.new("ShaderNodeSeparateXYZ")
    node_tree.links.new(coord.outputs["Object"], separate.inputs["Vector"])
    add_xy = node_tree.nodes.new("ShaderNodeMath")
    _sattr(add_xy, "operation", "ADD")
    node_tree.links.new(separate.outputs["X"], add_xy.inputs[0])
    node_tree.links.new(separate.outputs["Y"], add_xy.inputs[1])
    combine = node_tree.nodes.new("ShaderNodeCombineXYZ")
    node_tree.links.new(add_xy.outputs["Value"], combine.inputs["X"])
    node_tree.links.new(separate.outputs["Z"], combine.inputs["Y"])
    return combine.outputs["Vector"]


def _lerp_scalar(node_tree, factor_socket, value_a, value_b):
    """Linear-interpolate two scalar constants by a factor socket (0 -> value_a, 1 -> value_b),
    built from two Math nodes. Used for frame-vs-glass Roughness/Metallic in office_facade --
    a scalar doesn't need MixRGB's color-mixing machinery, and a generic ShaderNodeMix's
    per-data-type socket layout isn't confirmed identical across target Blender versions."""
    scale = node_tree.nodes.new("ShaderNodeMath")
    _sattr(scale, "operation", "MULTIPLY")
    node_tree.links.new(factor_socket, scale.inputs[0])
    scale.inputs[1].default_value = value_b - value_a
    add = node_tree.nodes.new("ShaderNodeMath")
    _sattr(add, "operation", "ADD")
    node_tree.links.new(scale.outputs["Value"], add.inputs[0])
    add.inputs[1].default_value = value_a
    return add.outputs["Value"]


def _texture_brick(node_tree, bsdf, spec):
    """TexBrick drives both Base Color (brick vs mortar coloring, blended against the preset's
    own base color) and, via its Factor/Fac output, a Bump for the mortar recess line. TexBrick's
    output socket is named 'Fac' on 4.2 and 'Factor' on 5.x (found via testing, not in the plan's
    original compat allowlist) -- candidate both."""
    base_rgba = tuple(bsdf.inputs["Base Color"].default_value)
    mortar = (0.75, 0.73, 0.68, 1.0)

    brick = node_tree.nodes.new("ShaderNodeTexBrick")
    _sock(brick, ["Scale"], 5.0)
    node_tree.links.new(_wall_uv(node_tree), brick.inputs["Vector"])
    brick.inputs["Color1"].default_value = base_rgba
    brick.inputs["Color2"].default_value = tuple(c * 0.85 for c in base_rgba[:3]) + (1.0,)
    brick.inputs["Mortar"].default_value = mortar
    factor_output = brick.outputs.get("Factor") or brick.outputs.get("Fac")

    varied = _add_color_variation(node_tree, brick.outputs["Color"])
    node_tree.links.new(varied, bsdf.inputs["Base Color"])

    bump = node_tree.nodes.new("ShaderNodeBump")
    _sock(bump, ["Strength"], 0.25)
    _sock(bump, ["Distance"], 0.2)
    node_tree.links.new(factor_output, bump.inputs["Height"])
    node_tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


def _texture_brushed(node_tree, bsdf, spec):
    """Anisotropic-looking brushed-metal streaks: a Mapping node stretches noise heavily along
    one axis before feeding TexNoise, so the resulting pattern reads as directional streaks
    rather than blobs, driving a subtle Roughness variation."""
    coord = node_tree.nodes.new("ShaderNodeTexCoord")
    mapping = node_tree.nodes.new("ShaderNodeMapping")
    _sattr(mapping, "vector_type", "TEXTURE")
    mapping.inputs["Scale"].default_value = (1.0, 40.0, 1.0)  # stretch along Y for streaks
    node_tree.links.new(coord.outputs["Object"], mapping.inputs["Vector"])

    noise = node_tree.nodes.new("ShaderNodeTexNoise")
    _sock(noise, ["Scale"], 20.0)
    _sock(noise, ["Detail"], 2.0)
    node_tree.links.new(mapping.outputs["Vector"], noise.inputs.get("Vector"))

    base_rough = bsdf.inputs["Roughness"].default_value
    rough_ramp = node_tree.nodes.new("ShaderNodeMapRange")
    _sattr(rough_ramp, "clamp", True)
    _sock(rough_ramp, ["To Min"], max(0.0, base_rough - 0.1))
    _sock(rough_ramp, ["To Max"], min(1.0, base_rough + 0.1))
    node_tree.links.new(noise.outputs["Fac"], rough_ramp.inputs["Value"])
    node_tree.links.new(rough_ramp.outputs["Result"], bsdf.inputs["Roughness"])


def _texture_marble(node_tree, bsdf, spec):
    """Classic procedural-marble recipe: a banded Wave texture whose Distortion input is driven
    by a Noise texture's Fac output (a spatially-varying distortion *amount*, not a coordinate
    override -- an earlier version of this function fed noise directly into Voronoi's Vector
    input, which collapsed the 3D coordinate to a 1D diagonal and rendered as a flat, undistorted
    single color; confirmed visually and fixed here), giving the turbulent, ropy vein look real
    marble has instead of Voronoi's cellular pattern (which reads as "cracked tile", not veining).
    TexWave's Fac/Factor output name diverges between Blender versions (4.2/5.x respectively --
    found via testing, not in the original compat allowlist)."""
    base_rgba = tuple(bsdf.inputs["Base Color"].default_value)
    vein_color = tuple(c * 0.35 for c in base_rgba[:3]) + (1.0,)

    noise = node_tree.nodes.new("ShaderNodeTexNoise")
    _sock(noise, ["Scale"], 1.5)
    _sock(noise, ["Detail"], 6.0)
    # TexNoise's Fac output is ~[0, 1] but TexWave's Distortion needs values in roughly the
    # 5-20 range to meaningfully break up the wave's regularity -- confirmed visually: feeding
    # Fac into Distortion unscaled produced clean, regular concentric rings (an "onion" look),
    # not the turbulent veining marble needs, because the distortion was too weak to matter.
    scale_distortion = node_tree.nodes.new("ShaderNodeMath")
    _sattr(scale_distortion, "operation", "MULTIPLY")
    scale_distortion.inputs[1].default_value = 18.0
    node_tree.links.new(noise.outputs["Fac"], scale_distortion.inputs[0])

    wave = node_tree.nodes.new("ShaderNodeTexWave")
    _sattr(wave, "wave_type", "BANDS")
    _sock(wave, ["Scale"], 4.0)
    _sock(wave, ["Detail"], 4.0)
    node_tree.links.new(scale_distortion.outputs["Value"], wave.inputs["Distortion"])
    wave_fac = wave.outputs.get("Fac") or wave.outputs.get("Factor")

    ramp = node_tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = vein_color
    ramp.color_ramp.elements[1].color = base_rgba
    ramp.color_ramp.elements[0].position = 0.35
    ramp.color_ramp.elements[1].position = 0.55
    node_tree.links.new(wave_fac, ramp.inputs["Fac"])

    varied = _add_color_variation(node_tree, ramp.outputs["Color"])
    node_tree.links.new(varied, bsdf.inputs["Base Color"])


def _texture_office_facade(node_tree, bsdf, spec):
    """Material-based window grid: a straight, non-offset TexBrick pattern (a real brick pattern
    staggers alternating rows; offset=0 makes it a plain aligned grid instead) splits the wall
    into frame-vs-glass regions across Base Color, Roughness, and Metallic, and a per-window-cell
    noise-driven mask drives Emission Strength so some windows read as lit and others dark. This
    is the single node setup with the biggest "reads as a building, not a box" payoff in the
    whole realism plan, at near-zero render cost -- no extra geometry at all, everything here is
    shading.

    Do this before the geometry version (actual window-shaped cutouts/insets): it's cheap, has no
    placement risk, and covers all four facades of a box from one material.
    """
    pitch_h = spec.get("pitch_h", 1.0)
    pitch_v = spec.get("pitch_v", 1.2)
    frame_color = spec.get("frame_color", (0.12, 0.12, 0.13, 1.0))
    lit_fraction = spec.get("lit_fraction", 0.35)
    glass_color = tuple(bsdf.inputs["Base Color"].default_value)
    glass_roughness = bsdf.inputs["Roughness"].default_value
    glass_metallic = bsdf.inputs["Metallic"].default_value

    wall_uv = _wall_uv(node_tree)
    mapping = node_tree.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (1.0 / pitch_h, 1.0 / pitch_v, 1.0)
    node_tree.links.new(wall_uv, mapping.inputs["Vector"])

    brick = node_tree.nodes.new("ShaderNodeTexBrick")
    _sattr(brick, "offset", 0.0)  # 0 = no row stagger -- a straight window grid, not brickwork
    _sock(brick, ["Scale"], 1.0)  # pitch is already baked into `mapping`'s scale above
    _sock(brick, ["Mortar Size"], 0.06)
    node_tree.links.new(mapping.outputs["Vector"], brick.inputs["Vector"])
    # frame_factor: 1.0 in the mortar/frame region, 0.0 in the brick/glass region (confirmed by
    # rendering, not assumed -- see fixtures/README.md's Phase 6 entry).
    frame_factor = brick.outputs.get("Factor") or brick.outputs.get("Fac")

    base_mix = _mix_color(node_tree, frame_factor, glass_color, frame_color)
    varied = _add_color_variation(node_tree, base_mix.outputs["Color"])
    node_tree.links.new(varied, bsdf.inputs["Base Color"])
    node_tree.links.new(_lerp_scalar(node_tree, frame_factor, glass_roughness, 0.5), bsdf.inputs["Roughness"])
    node_tree.links.new(_lerp_scalar(node_tree, frame_factor, glass_metallic, 0.6), bsdf.inputs["Metallic"])

    # Per-window lit/unlit mask: floor the (already pitch-scaled) wall coordinate to a
    # per-cell-CONSTANT value -- every pixel inside one window shares the exact same input to
    # cell_noise below, so the whole window lights up or stays dark as one unit, not a gradient.
    #
    # +0.5 after flooring is load-bearing, not cosmetic: confirmed by direct testing that
    # Blender's Noise texture is degenerate at exact integer coordinates -- a render feeding
    # noise pure floor() output (always an integer) came back as one perfectly flat, uniform
    # value across the *entire* surface (verified in isolation: a gradient of floor(x) values
    # from -5 to 4 fed straight into Emission Color rendered as one solid flat tone, not a
    # striped gradient). Every "cell" landed on the same degenerate lattice point and produced
    # identical noise, so the mask was constant everywhere -- either all windows lit or all dark,
    # which looked like "no emission at all" whenever the constant fell below the ramp threshold.
    # Sampling the cell *center* (floor + 0.5) instead of its corner avoids the lattice points
    # entirely and is also the more semantically correct thing to sample anyway.
    cell_sep = node_tree.nodes.new("ShaderNodeSeparateXYZ")
    node_tree.links.new(mapping.outputs["Vector"], cell_sep.inputs["Vector"])
    floor_x = node_tree.nodes.new("ShaderNodeMath")
    _sattr(floor_x, "operation", "FLOOR")
    node_tree.links.new(cell_sep.outputs["X"], floor_x.inputs[0])
    center_x = node_tree.nodes.new("ShaderNodeMath")
    _sattr(center_x, "operation", "ADD")
    node_tree.links.new(floor_x.outputs["Value"], center_x.inputs[0])
    center_x.inputs[1].default_value = 0.5
    floor_y = node_tree.nodes.new("ShaderNodeMath")
    _sattr(floor_y, "operation", "FLOOR")
    node_tree.links.new(cell_sep.outputs["Y"], floor_y.inputs[0])
    center_y = node_tree.nodes.new("ShaderNodeMath")
    _sattr(center_y, "operation", "ADD")
    node_tree.links.new(floor_y.outputs["Value"], center_y.inputs[0])
    center_y.inputs[1].default_value = 0.5
    cell_coord = node_tree.nodes.new("ShaderNodeCombineXYZ")
    node_tree.links.new(center_x.outputs["Value"], cell_coord.inputs["X"])
    node_tree.links.new(center_y.outputs["Value"], cell_coord.inputs["Y"])

    cell_noise = node_tree.nodes.new("ShaderNodeTexNoise")
    _sock(cell_noise, ["Scale"], 1.0)
    node_tree.links.new(cell_coord.outputs["Vector"], cell_noise.inputs["Vector"])

    lit_ramp = node_tree.nodes.new("ShaderNodeValToRGB")
    _sattr(lit_ramp.color_ramp, "interpolation", "CONSTANT")  # hard 0/1 split, not a gradient
    # CONSTANT interpolation holds the *lower* element's color across its whole segment up to
    # the next element's position (confirmed by direct testing: Fac=0.8 against elements at
    # (0.0, black) and (0.6, blue) rendered blue, not black) -- so the DARK color belongs on
    # element[0] at its default position 0.0, and the LIT color goes on element[1], moved up to
    # the threshold. An earlier version of this moved element[0] instead of element[1], which
    # left element[1] stranded at its default position 1.0 -- meaning practically every Fac
    # value (anything below 1.0) fell in element[0]'s segment and rendered dark, regardless of
    # lit_fraction. That bug made every single window read as unlit, which looked exactly like
    # "no emission is working at all" until isolated with a hardcoded-Fac test.
    lit_ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
    lit_ramp.color_ramp.elements[1].position = max(0.0, min(1.0, 1.0 - lit_fraction))
    lit_ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
    node_tree.links.new(cell_noise.outputs["Fac"], lit_ramp.inputs["Fac"])

    # Only the glass region emits (frame never glows): multiply the lit mask by (1 - frame_factor).
    not_frame = node_tree.nodes.new("ShaderNodeMath")
    _sattr(not_frame, "operation", "SUBTRACT")
    not_frame.inputs[0].default_value = 1.0
    node_tree.links.new(frame_factor, not_frame.inputs[1])
    window_mask = node_tree.nodes.new("ShaderNodeMath")
    _sattr(window_mask, "operation", "MULTIPLY")
    node_tree.links.new(lit_ramp.outputs["Color"], window_mask.inputs[0])
    node_tree.links.new(not_frame.outputs["Value"], window_mask.inputs[1])

    emission_strength_node = node_tree.nodes.new("ShaderNodeMath")
    _sattr(emission_strength_node, "operation", "MULTIPLY")
    node_tree.links.new(window_mask.outputs["Value"], emission_strength_node.inputs[0])
    emission_strength_node.inputs[1].default_value = spec.get("emission_strength", 4.0)
    node_tree.links.new(emission_strength_node.outputs["Value"], bsdf.inputs["Emission Strength"])
    bsdf.inputs["Emission Color"].default_value = spec.get("emission_color", (1.0, 0.9, 0.7, 1.0))


TEXTURE_BUILDERS = {
    "grungy_noise": _texture_grungy_noise,
    "brick": _texture_brick,
    "brushed": _texture_brushed,
    "office_facade": _texture_office_facade,
    "marble": _texture_marble,
}


def setup_objects(bl_scene, objects):
    for obj in objects:
        op_name = _PRIMITIVE_OPS[obj["primitive"]]
        tessellation = _TESSELLATION_KWARGS.get(obj["primitive"], {})
        getattr(bpy.ops.mesh, op_name)(location=obj["location"], **tessellation)
        bl_obj = bpy.context.active_object
        bl_obj.name = obj["name"]
        bl_obj.rotation_euler = obj["rotation"]
        bl_obj.scale = obj["scale"]

        # Bake scale into the mesh (verified headless: transform_apply requires no special
        # context override) so modifier widths below are in real world units instead of a
        # pre-scale local unit, texture coordinates in Object space (Phase 5+) don't stretch,
        # and obj.dimensions -- used immediately below for the bevel-width clamp -- is accurate.
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

        if obj["primitive"] in _BEVEL_PRIMITIVES:
            bevel = bl_obj.modifiers.new(name="AutoBevel", type="BEVEL")
            smallest_dim = min(bl_obj.dimensions) if bl_obj.dimensions else _BEVEL_WIDTH_MAX
            bevel.width = min(_BEVEL_WIDTH_MAX, smallest_dim * _BEVEL_WIDTH_DIMENSION_FRACTION)
            bevel.segments = _BEVEL_SEGMENTS
            _sattr(bevel, "limit_method", "ANGLE")
            _sattr(bevel, "angle_limit", math.radians(_BEVEL_ANGLE_LIMIT_DEG))
        else:
            # Rounded primitives get smooth shading instead of a bevel. shade_smooth_by_angle is
            # the modern (4.1+) operator; use_auto_smooth (the property it replaced) was removed
            # in 4.1 and must never be touched. Both the operator and a plain shade_smooth()
            # fallback are confirmed present on both target Blender versions (see
            # scripts/compat_report.md), so the fallback is defense-in-depth, not expected to fire.
            if hasattr(bpy.ops.object, "shade_smooth_by_angle"):
                bpy.ops.object.shade_smooth_by_angle(angle=math.radians(_SHADE_SMOOTH_ANGLE_DEG))
            else:
                bpy.ops.object.shade_smooth()

        material = obj["material"]

        if obj["primitive"] == "PLANE" and material["transmission"] > 0.0:
            # A PLANE has zero thickness -- real transmissive glass through a zero-thickness
            # surface refracts once with nothing to refract back out of, which looks wrong (flat,
            # no distortion/displacement) compared to an actual pane of glass. Blender 5.x's
            # Principled BSDF has a "Thin Wall" socket built for exactly this case, but it's
            # absent on the pinned prod version (4.2.0 -- see scripts/compat_report.md), so a
            # thin SOLIDIFY modifier is the version-safe substitute: it gives the pane real
            # (if minimal) thickness so transmission actually has two surfaces to refract through.
            solidify = bl_obj.modifiers.new(name="AutoSolidify", type="SOLIDIFY")
            _sattr(solidify, "thickness", 0.02)

        mat = bpy.data.materials.new(name=material["name"])
        mat.use_nodes = True
        bsdf = _node(mat.node_tree, "ShaderNodeBsdfPrincipled")
        for key, (candidates, is_color) in _MATERIAL_SOCKETS.items():
            _sock(bsdf, candidates, material[key], is_color=is_color)
        texture_builder = TEXTURE_BUILDERS.get(material.get("texture"))
        if texture_builder:
            texture_builder(mat.node_tree, bsdf, material.get("facade_spec", {}))
        bl_obj.data.materials.append(mat)


def render(scene, output_path):
    """Build and render one scene. `scene` is normalized internally (idempotent, so it's safe
    to pass either a raw model-authored scene graph or an already-normalized one -- see
    normalize_scene's docstring)."""
    norm = normalize_scene(scene)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bl_scene = bpy.context.scene

    setup_color_management(bl_scene, norm["render_settings"])
    setup_render_settings(bl_scene, norm["render_settings"])
    setup_world(bl_scene, norm["environment"])
    setup_camera(bl_scene, norm["camera"])
    setup_lights(bl_scene, norm["lighting"])
    setup_objects(bl_scene, norm["objects"])

    bl_scene.render.filepath = output_path
    bl_scene.render.image_settings.file_format = "PNG"

    if os.environ.get("RENDER_DRY_RUN") == "1":
        # Build the whole scene, skip the actual (slow) render call -- used by the local test
        # harness for a fast per-fixture sanity check (does the scene build without error at
        # all?) without spending render time on every iteration.
        print(f"RENDER_DRY_RUN set, skipping render. Scene built OK. output_path={output_path}")
        return

    bpy.ops.render.render(write_still=True)
    print(f"Render complete: {bl_scene.render.filepath}")


def _main():
    payload = globals().get("PAYLOAD")
    if payload is not None:
        scene = payload["scene"]
        output_path = payload["output_path"]
    else:
        argv = sys.argv
        argv = argv[argv.index("--") + 1:] if "--" in argv else []
        if len(argv) < 2:
            print(
                "Usage: blender --background --factory-startup --python blender_runtime.py "
                "-- scene.json output.png",
                file=sys.stderr,
            )
            sys.exit(1)
        with open(argv[0]) as f:
            scene = json.load(f)
        output_path = argv[1]
    render(scene, output_path)


if bpy is not None:
    _main()
