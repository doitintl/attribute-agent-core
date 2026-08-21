"""
3D Rendering SaaS Agent for AgentCore Runtime Instances (GPU).

A multi-tenant 3D rendering assistant that takes plain-language scene descriptions
and produces photorealistic renders using Blender Cycles with NVIDIA OptiX GPU
ray tracing. Built with Strands Agents SDK and Claude Opus 4.5.

Uses the bedrock_agentcore.BedrockAgentCoreApp SDK (matching the working ../agent/agent.py
and ../container-deploy agents in this repo) instead of a hand-rolled FastAPI app, which
gets the AgentCore HTTP contract right for free: POST /invocations (not /invoke) and a
GET /ping health check with the "Healthy"/"HealthyBusy" status values AgentCore expects.

Long-running renders: Blender jobs can take up to 30 minutes (see render_scene's subprocess
timeout below), but AgentCore tears down a session after 15 minutes of "Healthy" (idle-looking)
pings. Per https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html,
the entrypoint below runs the actual agent/render call on a background thread and wraps it in
add_async_task/complete_async_task, so /ping reports "HealthyBusy" for the whole render and the
session survives past the 15-minute idle mark. The client-facing contract is unchanged (one
invoke-agent-runtime call still blocks until the finished render is returned); what's fixed is
that a long render no longer looks idle to the platform.

Requires: AgentCore EC2 capacity provider with GPU instances (g5.xlarge+)
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime
from typing import Optional

import boto3
from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from strands import Agent, tool
from strands.models import BedrockModel

import blender_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("3d-render-agent")

# AgentCore app - handles the /invocations + /ping contract for us.
app = BedrockAgentCoreApp(debug=True)

# Configuration
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

# Header name for tenant ID (must be in requestHeaderAllowlist)
TENANT_ID_HEADER = "x-tenant-id"

# Output directory for renders
RENDER_OUTPUT_DIR = os.environ.get("RENDER_OUTPUT_DIR", "/tmp/renders")

# Scene-graph passthrough (payload carries scene_graph_json directly, skipping the LLM entirely)
# -- lets prod render byte-identical fixture JSON to what scripts/render_local.py runs locally,
# so any difference is attributable to the platform (real GPU/OPTIX/linux-x64 build) rather than
# to the model's whim, with zero Bedrock tokens spent. Behind an env flag (default on) rather
# than unconditional: it's a direct, LLM-bypassing entry into the renderer with only the
# existing budget caps as a backstop, worth being able to switch off without a redeploy.
SCENE_PASSTHROUGH_ENABLED = os.environ.get("SCENE_PASSTHROUGH_ENABLED", "true").lower() in ("1", "true", "yes")

# Where to upload finished renders so they're retrievable after the (ephemeral, per-session)
# EC2 instance is torn down -- /tmp is gone the moment the session ends. Empty bucket disables
# upload (render still succeeds, just isn't retrievable afterward -- useful for local testing
# where no bucket/permissions exist).
OUTPUT_S3_BUCKET = os.environ.get("OUTPUT_S3_BUCKET", "")
OUTPUT_S3_PREFIX = os.environ.get("OUTPUT_S3_PREFIX", "render3d-agent/outputs")

# Where the curated HDRI catalog (blender_runtime.HDRI_CATALOG) is pre-staged by Terraform
# (aws_s3_object.hdri) -- see hdri_assets/README.md. Defaults to the same deployment bucket the
# agent zip itself lives in, since it's already granted to this role.
HDRI_S3_BUCKET = os.environ.get("HDRI_S3_BUCKET", "agentcore-deployments-058264544288")
HDRI_S3_PREFIX = os.environ.get("HDRI_S3_PREFIX", "render3d-agent/hdri")
HDRI_CACHE_DIR = os.environ.get("HDRI_CACHE_DIR", "/tmp/hdri-cache")
_hdri_ready = {}  # name -> True | error message, tri-state cache per catalog name
_hdri_lock = threading.Lock()  # same atomic-staging-then-rename concurrency pattern as Blender's own install


def _ensure_hdri(name: str) -> tuple:
    """
    Make sure the named HDRI (a blender_runtime.HDRI_CATALOG key, already validated by
    normalize_scene before this is ever called) is downloaded and sha256-verified locally.

    Returns (local_path, None) on success, or (None, error_message) on any failure -- callers
    must treat failure as routine, not exceptional: a missing/corrupt HDRI should make the scene
    fall back to a procedural sky, never fail the render outright. Mirrors _ensure_blender's
    proven concurrency pattern (process lock, unique staging dir, atomic rename, tri-state cache)
    for exactly the same reason: concurrent render_scene calls in the same warm process must not
    race on the same shared download path.
    """
    entry = blender_runtime.HDRI_CATALOG.get(name)
    if not entry:
        return None, f"Unknown HDRI catalog name: {name!r}"

    cached = _hdri_ready.get(name)
    if cached is True:
        return os.path.join(HDRI_CACHE_DIR, entry["filename"]), None
    if isinstance(cached, str):
        return None, cached

    with _hdri_lock:
        cached = _hdri_ready.get(name)
        if cached is True:
            return os.path.join(HDRI_CACHE_DIR, entry["filename"]), None
        if isinstance(cached, str):
            return None, cached

        final_path = os.path.join(HDRI_CACHE_DIR, entry["filename"])
        if os.path.exists(final_path):
            _hdri_ready[name] = True
            return final_path, None

        os.makedirs(HDRI_CACHE_DIR, exist_ok=True)
        staging_path = os.path.join(HDRI_CACHE_DIR, f".staging-{uuid.uuid4().hex}-{entry['filename']}")
        try:
            s3_key = f"{HDRI_S3_PREFIX}/{entry['filename']}"
            boto3.client("s3", region_name=AWS_REGION).download_file(HDRI_S3_BUCKET, s3_key, staging_path)

            import hashlib
            digest = hashlib.sha256()
            with open(staging_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != entry["sha256"]:
                error = f"sha256 mismatch for HDRI {name!r}: expected {entry['sha256']}, got {digest.hexdigest()}"
                _hdri_ready[name] = error
                logger.error(error)
                return None, error

            if os.path.exists(final_path):
                pass  # another concurrent call already won the race -- use theirs
            else:
                try:
                    os.rename(staging_path, final_path)
                except OSError:
                    pass  # lost a tight race against another concurrent fetch -- fine, use theirs
        except Exception as exc:
            error = f"Failed to fetch HDRI {name!r} from s3://{HDRI_S3_BUCKET}/{s3_key}: {exc}"
            _hdri_ready[name] = error
            logger.exception(error)
            return None, error
        finally:
            if os.path.exists(staging_path):
                os.remove(staging_path)

        _hdri_ready[name] = True
        logger.info(f"HDRI {name!r} ready at {final_path}")
        return final_path, None


def _upload_render_to_s3(output_path: str, tenant_id: Optional[str]) -> Optional[str]:
    """Upload a finished render to S3, scoped under the given tenant. Returns the s3:// URI, or
    None if uploads are disabled (no bucket configured) or the upload fails.

    tenant_id is passed explicitly rather than pulled from thread-local/global state: a
    threading.local() attempt here was tested live and failed -- renders all landed under
    "unknown-tenant" regardless of which tenant's session triggered them, meaning whatever
    Strands does internally to dispatch a @tool call does not run on the same thread that set
    the thread-local (or copies context in a way that doesn't preserve it). See render_scene's
    factory function below for how tenant_id actually gets here: as a closure variable bound at
    Agent-build time, once per request, which sidesteps the question of which thread executes
    the tool entirely.
    """
    if not OUTPUT_S3_BUCKET:
        return None
    tenant_key = tenant_id or "unknown-tenant"
    key = f"{OUTPUT_S3_PREFIX}/{tenant_key}/{os.path.basename(output_path)}"
    try:
        boto3.client("s3", region_name=AWS_REGION).upload_file(output_path, OUTPUT_S3_BUCKET, key)
        uri = f"s3://{OUTPUT_S3_BUCKET}/{key}"
        logger.info(f"Uploaded render to {uri}")
        return uri
    except Exception as exc:
        logger.exception(f"Failed to upload render to S3: {exc}")
        return None

SYSTEM_PROMPT = """You are a 3D rendering agent for a SaaS platform.

Your job is to take plain-language scene descriptions from users and produce
photorealistic rendered images using GPU-accelerated ray tracing.

WORKFLOW:
1. Receive a scene description from the user
2. Call generate_scene_description to see the scene JSON schema (it's a format reference, not
   a real parser -- see its docstring). Then AUTHOR YOUR OWN scene_graph_json reflecting what
   the user actually asked for, following the renderer contract below.
3. Call render_scene exactly once with that JSON. Only call it again if the returned JSON has
   "status": "error" -- a successful render never needs a retry.
4. Report back using ONLY values from render_scene's actual returned JSON: include render_time,
   resolution, and s3_uri if present. Never claim a rendering error unless the tool's own
   "status" field says "error" -- the render can succeed even when it doesn't look like what
   you imagined; report what actually happened, not what you expected.

RENDERER CONTRACT (this is the entire toolkit -- nothing outside this list exists):
- Primitives: CUBE, PLANE, SPHERE, CYLINDER, CONE only. No custom meshes. Build compound shapes
  (a "car", a "castle tower", a "skyscraper") by combining several primitives of different
  scale/position/rotation/color -- there is no single primitive that looks like any of those on
  its own.
- Objects can be rotated: optional objects[].rotation is [x, y, z] in DEGREES (a cylinder lying
  on its side, a cube tilted 15 degrees). Omit it for the common case (upright, axis-aligned) --
  a scene doesn't need every object rotated to look intentional.
- Edges and corners are automatically softened (a small bevel on cubes, smooth shading on
  spheres/cylinders/cones) -- you don't need to do anything for this, it's automatic.
- Scale is a multiplier on a 2-unit base size: scale [1,1,1] = a 2x2x2 cube. A skyscraper does
  NOT need scale 5 to look tall -- scale 1.5-2 already reads as a tower once the camera and
  other objects are sized consistently around it.
- Materials: prefer material.preset over hand-picking base_color/metallic/roughness -- presets
  carry real procedural surface texture (concrete grain, brick coursing, brushed-metal streaks,
  marble veining), not just a flat color. Presets: "concrete", "weathered_concrete", "asphalt",
  "rust", "brick", "brushed_metal", "marble", "chrome", "car_paint", "glass", "tinted_glass",
  "frosted_glass", "window_glass" (an opaque reflective facade look -- use this for "glass
  building/skyscraper", not "glass", which is real transmissive glass meant for small objects
  like a bottle or a car windshield, not a whole facade), "office_facade" (a real window grid --
  frame vs glass regions, with some windows lit and some dark, driven entirely by the material;
  use this for "office building"/"apartment tower"/any building described with visible windows,
  and especially for a *night* scene -- the lit/unlit window pattern is the single strongest
  "this is a building, not a box" cue available), "water", "neon", "lit_window". Any field you
  also set explicitly (base_color, metallic, roughness, specular, ior, transmission, alpha,
  emission_color, emission_strength, coat_weight) overrides that preset's value -- e.g.
  material.preset "brick" with an explicit base_color tints the brick red/grey/etc without
  losing the brick pattern. office_facade also takes pitch_h/pitch_v (window spacing in world
  units, default 1.0/1.2) and lit_fraction (0-1, default 0.35, fraction of windows lit) -- don't
  make pitch_h/pitch_v larger than the object's own scale or you'll only see one giant "window".
  Without a preset, base_color/metallic/roughness/specular still work exactly as before (flat
  color, no texture).
- Real transmissive glass ("glass", "tinted_glass", "frosted_glass", "water" presets, or any
  material with transmission > 0) automatically gets the extra light-bounce budget it needs --
  you don't need to request this. render_settings.glass_quality: "high" additionally enables
  refractive caustics and a samples boost, at real render-time cost -- only ask for it when the
  scene specifically calls for prominent glass (a glass sculpture, a wine glass), not for
  ordinary window glass.
- Use 5-15 objects for anything described as a scene, city, or complex structure (up to 60 --
  anything beyond that is silently capped). Two objects (a subject + a ground plane) is the
  floor, not the target -- vary height/scale/color across objects to suggest a skyline, a crowd,
  a cluttered room, etc.
- render_settings.samples is capped at 4096 and resolution at 4096x4096 -- there's no benefit to
  requesting more, it will just be clamped.

CAMERA -- set camera.preset to one of "wide" (city skylines, multi-object scenes), "standard"
(single subject + ground, the proven default), "close" (product shots, one small subject),
"hero_low" (dramatic low-angle looking up -- imposing/heroic), "aerial" (near-overhead --
skylines, layouts), "product" (tight, centered, one small subject). Do not invent your own
rotation math (Euler angles are easy to get wrong) -- every preset automatically points at the
world origin, so you never need to set rotation at all. If geometry isn't centered at the
origin, set camera.look_at to the point you actually want in frame instead (any explicit
camera.rotation you provide still overrides both). These presets were tuned against ~2-unit-scale
objects -- for anything noticeably taller (a skyscraper with scale 3+, i.e. 6+ units tall), set
camera.location explicitly further back and higher, e.g. [18.0, -15.0, 9.0] with look_at [0,0,0],
to keep the whole structure and some sky in frame instead of cropping to a close-up of one corner.

For a shallow-focus/bokeh look (a hero product shot, a close portrait), set
camera.dof_enabled: true with a low f_stop (0.5-2.0 for strong blur, 4-8 for subtle). Without an
explicit camera.focus_distance, focus automatically lands on whatever camera.look_at points at
-- only set focus_distance yourself for an intentionally off-target focus point.

ENVIRONMENT -- prefer environment.type "SKY" with a named environment.preset over "COLOR" with
hand-picked color/strength values: SKY renders an actual physically-based sky (via Blender's
Nishita sun/atmosphere model) that lights the scene directionally and puts a real gradient in
frame, not a flat constant -- this is the single biggest lever on whether a render looks
photographic or looks like flat-lit plastic blocks. Presets: "noon", "golden_hour", "sunset",
"blue_hour", "overcast", "dawn". Two presets are deliberately NOT a sky (the sun's below the
horizon or there is no sun) and render as a flat COLOR instead, same as before: "night_city" and
"studio" -- for those, still compensate with a point or area light at energy 2000+ near the
subject, since a dark environment with only default light energies renders as an unlit blur.
Manual key/fill lights are still useful as *accents* (e.g. a colored rim light) but shouldn't be
the primary light source when a SKY preset is active -- a single overpowering manual light hides
the sky's directional shading entirely (confirmed: an 1800-energy manual key light fully masked
the "sunset" preset's effect in testing). Explicit environment.color/strength still work exactly
as before if you set type "COLOR" instead -- useful for stylized/non-realistic requests.

For real photographic reflections/lighting (a chrome/mirror/glossy subject where the reflection
itself matters, or a request for genuinely photoreal quality), use environment.type "HDRI" with
environment.preset set to one of a fixed catalog: "clear_sky", "golden_sunset", "overcast",
"night_sky", "studio", "field" -- these are curated real photographed environments, not
generative, so there is no way to request a URL or a custom HDRI; an unrecognized name silently
falls back to the closest SKY preset. Prefer SKY for anything else -- it's cheaper and covers
the same moods procedurally.

LIGHTING: AREA and SPOT lights automatically aim at the world origin (or an optional explicit
lighting[].aim_at [x,y,z]) -- you do not need to compute a rotation, an offset key light at
[6,-1,3] now correctly lights a subject at the origin instead of just lighting the ground below
itself. SPOT lights also take spot_size_deg (cone angle, default 45) and spot_blend (edge
softness 0-1, default 0.15). SUN lights take elevation_deg/azimuth_deg and, if omitted, inherit
the active SKY preset's own sun angle so cast shadows point the same way the visible sun does.
If you don't want to hand-author lighting[] at all, set lighting_rig to one of "studio_3point",
"dramatic_rim", "overcast_soft", "night_street" instead -- it fills in a competent 1-3 light
setup for you (ignored if lighting[] is non-empty).

GUIDELINES:
- Be creative in interpreting vague descriptions, within the renderer contract above
- Default to photorealistic quality unless told otherwise
- Report render time and resolution in your response
"""

# Initialize the model
model = BedrockModel(model_id=BEDROCK_MODEL_ID, region_name=AWS_REGION)

# Tenant attribution for Bedrock invocation logging (requestMetadata) -- keyed "x-tenant-id" to
# match the AgentCore-layer header, so both log sources can be grepped/joined on the same key.
# Plain module global, not thread-local: per the current deployment model, one instance serves
# exactly one tenant for its whole lifetime, so there's no cross-tenant race to guard against --
# see get_tenant_id_from_headers/invoke() below, which sets this once tenant_id is resolved.
# Note: requestMetadata is only durably observable if Bedrock model invocation logging is
# enabled for this account/region -- it's set on every Converse call either way, but nothing
# persists it otherwise.
_instance_tenant_id: Optional[str] = None


def _inject_tenant_metadata(params, **kwargs):
    if _instance_tenant_id:
        params["requestMetadata"] = {"x-tenant-id": _instance_tenant_id}


model.client.meta.events.register("before-parameter-build.bedrock-runtime.Converse", _inject_tenant_metadata)
model.client.meta.events.register("before-parameter-build.bedrock-runtime.ConverseStream", _inject_tenant_metadata)


@tool
def generate_scene_description(prompt: str) -> str:
    """
    Return a TEMPLATE scene graph showing the valid schema and field ranges -- this does NOT
    parse or interpret the prompt (the returned scene is a fixed reference example regardless
    of prompt content). Use its structure/fields as a format reference, then author your own
    scene_graph_json for render_scene that actually reflects the user's request, following the
    RENDERER CONTRACT in your system prompt.

    Args:
        prompt: Natural language scene description from the user (echoed into metadata only,
            not used to generate the template)

    Returns:
        JSON string containing an example scene graph in the correct schema
    """
    scene_graph = {
        "metadata": {
            "description": prompt,
            "generated_at": datetime.utcnow().isoformat(),
            "render_engine": "cycles",
            "device": "OPTIX",
        },
        "camera": {
            "preset": "standard",
            "focal_length": 50,
            "sensor_width": 36,
        },
        "lighting": [
            {
                "type": "AREA",
                "name": "KeyLight",
                "location": [4.0, -3.0, 6.0],
                "energy": 1000,
                "size": 5.0,
                "color": [1.0, 0.95, 0.9],
            },
            {
                "type": "AREA",
                "name": "FillLight",
                "location": [-3.0, 2.0, 4.0],
                "energy": 300,
                "size": 3.0,
                "color": [0.9, 0.95, 1.0],
            },
            {
                "type": "POINT",
                "name": "RimLight",
                "location": [-2.0, -5.0, 3.0],
                "energy": 500,
                "color": [1.0, 1.0, 1.0],
            },
        ],
        "environment": {
            "type": "SKY",
            "preset": "golden_hour",
        },
        "objects": [
            {
                "name": "MainSubject",
                "type": "MESH",
                "primitive": "CUBE",
                "location": [0.0, 0.0, 1.0],
                "scale": [2.0, 1.0, 0.5],
                "rotation": [0.0, 0.0, 15.0],
                "material": {
                    "name": "SubjectMaterial",
                    "base_color": [0.8, 0.1, 0.1],
                    "metallic": 0.9,
                    "roughness": 0.1,
                    "specular": 0.8,
                },
            },
            {
                "name": "Ground",
                "type": "MESH",
                "primitive": "PLANE",
                "location": [0.0, 0.0, 0.0],
                "scale": [20.0, 20.0, 1.0],
                "material": {
                    "name": "GroundMaterial",
                    "preset": "asphalt",
                },
            },
        ],
        "render_settings": {
            "engine": "CYCLES",
            "device": "GPU",
            "samples": 256,
            "resolution_x": 1920,
            "resolution_y": 1080,
            "use_denoising": True,
            "denoiser": "OPTIX",
        },
    }

    return json.dumps(scene_graph, indent=2)


# Blender install, done lazily at runtime instead of baked into a Docker image -- this lets
# the agent run as a zip-based (codeConfiguration) artifact on the same GPU capacity provider,
# to test whether AgentCore's container-launch path (the thing that's actually crashing, see
# CURRENT_ISSUE.md/INVESTIGATION_LOG.md) is bypassed entirely when there's no container at all.
# Since this still runs on a real EC2 instance (not a sandboxed microVM), a lazy download+
# extract works the same way the Dockerfile's RUN steps did at build time -- just deferred to
# first use, and once per instance rather than once per image build.
BLENDER_VERSION = "4.2.0"
BLENDER_INSTALL_DIR = os.environ.get("BLENDER_INSTALL_DIR", "/tmp/blender-install")
BLENDER_BIN = os.path.join(BLENDER_INSTALL_DIR, f"blender-{BLENDER_VERSION}-linux-x64", "blender")
_blender_ready = None  # tri-state cache: None=unchecked, True=ready, str=error message
# Guards the whole install-if-needed sequence (Blender download/extract + shared-lib fetch).
# Without this, concurrent render_scene calls in the same process race on the same shared temp
# download paths (observed directly: overlapping installs corrupted each other's
# blender.tar.xz, producing "xz: Unexpected end of input" / "tar: Unexpected EOF in archive").
_install_lock = threading.Lock()


def _ensure_blender() -> Optional[str]:
    """
    Make sure a `blender` binary is available, installing it on first use if needed.

    Returns None if Blender is ready to use, or an error message string if it isn't (missing
    system shared libraries on this host are a real possibility here, since this path skips
    the Dockerfile's apt-get install of libgl1/libxi6/etc. -- surface that clearly rather than
    letting subprocess.run fail opaquely).
    """
    global _blender_ready, BLENDER_BIN
    if _blender_ready is True:
        return None
    if isinstance(_blender_ready, str):
        return _blender_ready

    with _install_lock:
        # Re-check now that we hold the lock -- another thread may have finished (or failed)
        # the install while we were waiting.
        if _blender_ready is True:
            return None
        if isinstance(_blender_ready, str):
            return _blender_ready
        return _install_blender_locked()


def _install_blender_locked() -> Optional[str]:
    """The actual install-and-verify sequence. Only ever called while holding _install_lock."""
    global _blender_ready, BLENDER_BIN

    system_blender = shutil.which("blender")
    candidate = system_blender or BLENDER_BIN

    if not os.path.exists(candidate):
        # Despite _install_lock, "Blender not found, installing" has been observed firing from
        # more than one concurrent invocation for the same instance (AgentCore/the SDK appears
        # to allow more than one in-flight request against a warm process, possibly across
        # separate handling contexts that don't share this module's lock). Rather than chase
        # that mechanism, make the install itself atomic and idempotent: download+extract to a
        # unique staging dir, then atomically rename into place. If BLENDER_INSTALL_DIR already
        # exists by the time we're ready to rename, another attempt already won -- discard our
        # staging copy and use theirs rather than erroring.
        logger.info(f"Blender not found at {candidate}, installing {BLENDER_VERSION}...")
        staging_dir = tempfile.mkdtemp(prefix="blender-staging-", dir=tempfile.gettempdir())
        archive_path = os.path.join(tempfile.gettempdir(), f"blender-{uuid.uuid4().hex}.tar.xz")
        try:
            url = f"https://download.blender.org/release/Blender4.2/blender-{BLENDER_VERSION}-linux-x64.tar.xz"
            subprocess.run(["wget", "-q", url, "-O", archive_path], check=True, timeout=300)
            subprocess.run(["tar", "-xf", archive_path, "-C", staging_dir], check=True, timeout=120)
            if os.path.isdir(BLENDER_INSTALL_DIR):
                shutil.rmtree(staging_dir, ignore_errors=True)  # someone else already won
            else:
                try:
                    os.rename(staging_dir, BLENDER_INSTALL_DIR)
                except OSError:
                    # Lost a tight race against another concurrent install -- fine, use theirs.
                    shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception as exc:
            _blender_ready = f"Failed to install Blender: {exc}"
            logger.exception(_blender_ready)
            shutil.rmtree(staging_dir, ignore_errors=True)
            return _blender_ready
        finally:
            if os.path.exists(archive_path):
                os.remove(archive_path)
        candidate = BLENDER_BIN

    lib_error = _ensure_shared_libs()
    if lib_error:
        _blender_ready = lib_error
        return lib_error

    try:
        result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            _blender_ready = f"blender --version failed (exit {result.returncode}): {result.stderr[-500:]}"
            logger.error(_blender_ready)
            return _blender_ready
    except Exception as exc:
        _blender_ready = f"Failed to run blender --version (likely missing shared libraries on this host): {exc}"
        logger.exception(_blender_ready)
        return _blender_ready

    BLENDER_BIN = candidate  # resolved path (system or freshly installed)
    _blender_ready = True
    logger.info(f"Blender ready at {candidate}")
    return None


# Shared libraries Blender needs that the AMI doesn't ship by default -- confirmed via `ldd`
# against a live instance. Everything else Blender needs is either already on the AMI or bundled
# in Blender's own lib/ directory. On a Docker-based deployment these came from `apt-get install`
# in the Dockerfile; here there's no image layer to bake them into, and the managed runtime
# process has no root/sudo, so they're fetched (unprivileged `dnf download` works fine -- it only
# needs network + a writable scratch dir, not install permissions) and extracted from their RPMs
# directly, once per instance, into LD_LIBRARY_PATH.
#
# Note: the original list (libXrender/libXi/libxkbcommon/libSM/libICE) was curated against a
# GPU capacity provider's AMI, which apparently ships libX11 preinstalled (likely pulled in by
# the NVIDIA driver package). A CPU-only capacity provider's AMI does not: `blender --version`
# there fails outright with exit 127, "error while loading shared libraries: libX11.so.6: cannot
# open shared object file" -- Blender can't even report its version, let alone render. libX11 is
# added here so this list is correct on both AMI types rather than only the one it was derived on.
MISSING_SHARED_LIBS = ["libX11", "libXrender", "libXi", "libxkbcommon", "libSM", "libICE"]
SHARED_LIB_DIR = os.environ.get("SHARED_LIB_DIR", "/tmp/blender-shared-libs")
_shared_libs_ready = None  # tri-state cache, same pattern as _blender_ready


def _ensure_shared_libs() -> Optional[str]:
    """Fetch and extract MISSING_SHARED_LIBS via dnf, add them to LD_LIBRARY_PATH."""
    global _shared_libs_ready
    if _shared_libs_ready is True:
        return None
    if isinstance(_shared_libs_ready, str):
        return _shared_libs_ready

    lib64_dir = os.path.join(SHARED_LIB_DIR, "usr", "lib64")
    if not os.path.isdir(lib64_dir):
        # Same atomic-staging-then-rename pattern as Blender's own install above, and for the
        # same reason: concurrent invocations have been observed racing on this despite
        # _install_lock.
        logger.info(f"Fetching missing shared libraries via dnf: {MISSING_SHARED_LIBS}")
        staging_dir = tempfile.mkdtemp(prefix="blender-libs-staging-", dir=tempfile.gettempdir())
        rpm_dir = tempfile.mkdtemp(prefix="blender-lib-rpms-", dir=tempfile.gettempdir())
        try:
            subprocess.run(
                ["dnf", "download", f"--destdir={rpm_dir}", *MISSING_SHARED_LIBS],
                check=True, capture_output=True, text=True, timeout=60,
            )
            for name in os.listdir(rpm_dir):
                if not name.endswith(".rpm"):
                    continue
                rpm_path = os.path.join(rpm_dir, name)
                with open(rpm_path, "rb") as rpm_file:
                    cpio = subprocess.Popen(["rpm2cpio", "/dev/stdin"], stdin=rpm_file, stdout=subprocess.PIPE)
                    subprocess.run(
                        ["cpio", "-idm", "--quiet"], stdin=cpio.stdout, cwd=staging_dir,
                        check=True, capture_output=True, timeout=30,
                    )
                    cpio.wait(timeout=30)
            if os.path.isdir(SHARED_LIB_DIR):
                shutil.rmtree(staging_dir, ignore_errors=True)  # someone else already won
            else:
                try:
                    os.rename(staging_dir, SHARED_LIB_DIR)
                except OSError:
                    shutil.rmtree(staging_dir, ignore_errors=True)  # lost the race, that's fine
        except Exception as exc:
            _shared_libs_ready = f"Failed to fetch shared libraries via dnf: {exc}"
            logger.exception(_shared_libs_ready)
            shutil.rmtree(staging_dir, ignore_errors=True)
            return _shared_libs_ready
        finally:
            shutil.rmtree(rpm_dir, ignore_errors=True)

    if not os.path.isdir(lib64_dir):
        _shared_libs_ready = f"dnf download/extract completed but {lib64_dir} was not created"
        logger.error(_shared_libs_ready)
        return _shared_libs_ready

    existing = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{lib64_dir}:{existing}" if existing else lib64_dir
    _shared_libs_ready = True
    logger.info(f"Shared libraries ready at {lib64_dir}")
    return None


def _render_scene_impl(scene_graph_json: str, output_filename: str, tenant_id: Optional[str]) -> str:
    """
    The actual render implementation, as a plain function -- shared by the @tool the model
    calls (see _make_render_scene_tool below) and invoke()'s scene-graph passthrough (which
    calls this directly, bypassing both the LLM and the @tool decoration, so prod can render
    byte-identical fixture JSON to what scripts/render_local.py runs locally; see the
    SCENE_PASSTHROUGH_ENABLED section of invoke()).
    """
    blender_error = _ensure_blender()
    if blender_error:
        return json.dumps({"status": "error", "error": blender_error}, indent=2)

    try:
        scene = json.loads(scene_graph_json)
    except json.JSONDecodeError as exc:
        # The old code did a bare json.loads() here, which raised straight out of the tool
        # call on any malformed model-authored JSON instead of returning a clean error the
        # model could see and react to.
        return json.dumps({"status": "error", "error": f"Invalid scene_graph_json: {exc}"}, indent=2)

    # Normalized once here (report resolution/samples/warnings from this, not the raw model
    # JSON -- the old code did `scene['render_settings']['resolution_x']` directly, which
    # KeyError'd whenever the model omitted render_settings even though the generator itself
    # tolerated that). blender_runtime.render() normalizes again internally from the same
    # pure function, so the two can never disagree.
    norm = blender_runtime.normalize_scene(scene)

    # HDRI fetch happens here, agent-side (Blender's own bundled Python has no boto3) --
    # normalize_scene already validated the catalog name; this resolves it to an actual
    # local file. Mutates the *raw* scene dict (not `norm`) before it's embedded into the
    # render script, since blender_runtime.render() re-normalizes internally and needs to
    # see the same resolved path. On any fetch failure, rewrite to the catalog's own
    # fallback_sky_preset here too -- belt #1 of "a missing HDRI must never fail a render"
    # (belt #2 is setup_world's own try/except around the actual image load, for the rarer
    # case where the file exists locally but fails to load).
    if norm["environment"]["type"] == "HDRI":
        hdri_name = norm["environment"]["preset"]
        local_path, hdri_error = _ensure_hdri(hdri_name)
        env = scene.setdefault("environment", {})
        if local_path:
            env["_hdri_local_path"] = local_path
        else:
            logger.warning(f"HDRI {hdri_name!r} unavailable ({hdri_error}), falling back to SKY")
            fallback = blender_runtime.HDRI_CATALOG.get(hdri_name, {}).get("fallback_sky_preset", "noon")
            env["type"] = "SKY"
            env["preset"] = fallback
            norm["_warnings"].append(f"HDRI {hdri_name!r} unavailable, used SKY preset {fallback!r} instead")

    output_filename = blender_runtime.safe_filename(output_filename or f"render_{uuid.uuid4().hex[:8]}")
    os.makedirs(RENDER_OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(RENDER_OUTPUT_DIR, output_filename)

    blender_script = _build_blender_script(scene, output_path)

    script_path = os.path.join(tempfile.gettempdir(), f"scene_{uuid.uuid4().hex[:8]}.py")
    with open(script_path, "w") as f:
        f.write(blender_script)

    logger.info(f"Executing Blender render: {output_path}")
    start_time = datetime.utcnow()

    try:
        result = subprocess.run(
            # --factory-startup: run against Blender's pristine default preferences rather
            # than whatever's on disk in this profile, for determinism between runs on the
            # same instance and parity with local testing (see scripts/render_local.py).
            [BLENDER_BIN, "--background", "--factory-startup", "--python", script_path],
            capture_output=True,
            text=True,
            timeout=1800,
        )

        elapsed = (datetime.utcnow() - start_time).total_seconds()

        if result.stdout:
            logger.info(f"Blender stdout: {result.stdout[-500:]}")
        if result.stderr:
            logger.warning(f"Blender stderr: {result.stderr[-500:]}")

        if os.path.exists(output_path):
            device_info = _parse_render_device(result.stdout or "")
            engine_label = (
                f"Cycles ({device_info['device_type']} GPU)"
                if device_info.get("gpu")
                else "Cycles (CPU)"
            )
            render_result = {
                "status": "success",
                "output_path": output_path,
                "s3_uri": _upload_render_to_s3(output_path, tenant_id),
                "render_time_seconds": round(elapsed, 2),
                "resolution": f"{norm['render_settings']['resolution_x']}x{norm['render_settings']['resolution_y']}",
                "samples": norm["render_settings"]["samples"],
                "engine": engine_label,
            }
            if norm["_warnings"]:
                render_result["warnings"] = norm["_warnings"]
        else:
            render_result = {
                "status": "error",
                "error": result.stderr[-500:] if result.stderr else "Unknown render error",
                "render_time_seconds": round(elapsed, 2),
            }

    except subprocess.TimeoutExpired:
        render_result = {"status": "error", "error": "Render timed out (exceeded 30 minutes)"}
    except FileNotFoundError:
        render_result = {"status": "error", "error": "Blender not found"}
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)

    return json.dumps(render_result, indent=2)


def _make_render_scene_tool(tenant_id: Optional[str]):
    """
    Build a render_scene @tool bound to a specific tenant_id via closure, delegating to
    _render_scene_impl above.

    render_scene needs to know which tenant's S3 prefix to upload the finished render under,
    but it's a @tool the model calls -- it can't be trusted to pass its own tenant_id argument
    (it doesn't know one), and a thread-local/global side channel was tried and confirmed broken
    live: renders consistently landed under "unknown-tenant" regardless of which tenant's
    session triggered them, meaning tool execution doesn't reliably happen on the thread that set
    the thread-local. Building a fresh closure per request (build_agent already constructs a new
    Agent per request anyway) sidesteps the question of which thread runs the tool entirely --
    tenant_id is just a captured variable, not shared mutable state.
    """

    @tool
    def render_scene(scene_graph_json: str, output_filename: str = "") -> str:
        """
        Execute GPU-accelerated rendering via Blender Cycles with NVIDIA OptiX.

        Args:
            scene_graph_json: JSON string containing the scene graph
            output_filename: Optional output filename (auto-generated if empty)

        Returns:
            JSON string with render result including output path and timing
        """
        return _render_scene_impl(scene_graph_json, output_filename, tenant_id)

    return render_scene


_BLENDER_RUNTIME_SOURCE = None


def _blender_runtime_source() -> str:
    """Read blender_runtime.py's own source text once and cache it. Read from disk (not
    inspect.getsource) so this works identically whether agent.py is running from the repo or
    from the unpacked zip artifact -- both lay the two files down as siblings."""
    global _BLENDER_RUNTIME_SOURCE
    if _BLENDER_RUNTIME_SOURCE is None:
        runtime_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_runtime.py")
        with open(runtime_path) as f:
            _BLENDER_RUNTIME_SOURCE = f.read()
    return _BLENDER_RUNTIME_SOURCE


def _build_blender_script(scene: dict, output_path: str) -> str:
    """
    Hand the scene graph to blender_runtime.py as a JSON payload instead of splicing values
    into Python source via f-strings (what the old generator did). json.dumps *then* repr is
    the load-bearing detail: json.dumps alone emits `true`/`null`/`false`, which are NameErrors
    as bare Python tokens, so json.dumps's output alone is not valid Python wherever a scene
    contains a bool or null. Wrapping the whole JSON string in repr() turns it into a single
    Python string literal -- always valid Python no matter what's inside (quotes, newlines,
    backslashes, unicode), and still exactly what json.loads expects once that literal
    evaluates. This is what makes a `"` in a model-authored object name, or the string value
    "true", harmless -- the old f-string generator broke on both.

    blender_runtime.render() normalizes the scene again internally, so this can safely carry
    the raw, unnormalized scene graph exactly as the model wrote it.
    """
    payload = json.dumps({"scene": scene, "output_path": output_path}, ensure_ascii=True)
    return f"import json\nPAYLOAD = json.loads({payload!r})\n" + _blender_runtime_source()


def _parse_render_device(stdout: str) -> dict:
    """Parse the `RENDER_DEVICE {...}` marker line blender_runtime.py prints, instead of the old
    `"GPU" in result.stdout` check -- that check was *always* true, because the CPU-fallback
    path's own log message contains the substring "GPU" ("GPU not available, using CPU
    rendering"), so every CPU render was unconditionally misreported as a GPU render."""
    for line in stdout.splitlines():
        if line.startswith("RENDER_DEVICE "):
            try:
                return json.loads(line[len("RENDER_DEVICE "):])
            except json.JSONDecodeError:
                pass
    return {"gpu": False, "device_type": None}


def build_agent(tenant_id: str = None, quality: str = "photorealistic") -> Agent:
    """Build a 3D render agent."""
    quality_instruction = (
        "Use high samples (256+) and OPTIX denoising for photorealistic output."
        if quality == "photorealistic"
        else "Use low samples (64) for fast draft preview."
    )
    
    system_prompt = f"{SYSTEM_PROMPT}\n\nQuality: {quality_instruction}"
    
    if tenant_id:
        system_prompt += f"\n\nYou are rendering on behalf of tenant: {tenant_id}"
    
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[generate_scene_description, _make_render_scene_tool(tenant_id)],
    )


def get_tenant_id_from_headers(headers: dict) -> Optional[str]:
    """
    Extract tenant_id from request headers.
    Headers are case-insensitive, so check common variations.
    """
    if not headers:
        return None

    if TENANT_ID_HEADER in headers:
        return headers[TENANT_ID_HEADER]

    lower_headers = {k.lower(): v for k, v in headers.items()}
    header_lower = TENANT_ID_HEADER.lower()

    if header_lower in lower_headers:
        return lower_headers[header_lower]

    for key in ["x-tenant-id", "x_tenant_id", "tenant-id", "tenant_id"]:
        if key in lower_headers:
            return lower_headers[key]

    return None


@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    """
    AgentCore entrypoint for the 3D render agent.

    Tenant ID resolution order:
    1. x-tenant-id header (preferred - requires requestHeaderAllowlist)
    2. tenant_id in payload body (fallback for backward compatibility)

    Expected payload format:
    {
        "prompt": "A red cube on a dark floor"
    }

    The actual render (agent + Blender subprocess, up to 30 minutes) runs on a background
    thread wrapped in add_async_task/complete_async_task, so /ping reports "HealthyBusy" for
    the duration instead of the session getting torn down at the 15-minute idle timeout. See
    the module docstring and
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html.
    """
    logger.info(f"Received payload: {json.dumps(payload)}")

    request_headers = context.request_headers if context else {}
    logger.info(f"Request headers: {json.dumps(request_headers)}")

    session_id = payload.get("session_id", "unknown")
    tenant_id = get_tenant_id_from_headers(request_headers)
    if not tenant_id:
        tenant_id = payload.get("tenant_id") or payload.get("x_tenant_id")

    global _instance_tenant_id
    _instance_tenant_id = tenant_id

    if payload.get("scene_graph_json") is not None:
        if not SCENE_PASSTHROUGH_ENABLED:
            return {"error": "scene_graph_json passthrough is disabled on this deployment"}
        # No LLM call, no Bedrock tokens -- render the scene graph exactly as given. Runs on the
        # same background-thread/add_async_task path as the normal flow so /ping still reports
        # HealthyBusy for the render's duration.
        task_id = app.add_async_task("3d_render_passthrough", {"tenant_id": tenant_id, "session_id": session_id})
        try:
            result_json = _render_scene_impl(
                payload["scene_graph_json"], payload.get("output_filename", ""), tenant_id
            )
        finally:
            app.complete_async_task(task_id)
        return {
            "agent": "3d-render",
            "tenant_id": tenant_id,
            "session_id": session_id,
            "mode": "scene_graph_passthrough",
            "response": result_json,
            "timestamp": datetime.utcnow().isoformat(),
        }

    prompt = payload.get("prompt") or payload.get("task") or payload.get("message")
    quality = payload.get("quality", "photorealistic")

    if not prompt:
        return {
            "error": "No prompt provided",
            "usage": "Send {'prompt': 'A red cube'} with an optional x-tenant-id header",
        }

    logger.info(f"Processing render request for tenant={tenant_id}, quality={quality}")

    task_id = app.add_async_task("3d_render", {"tenant_id": tenant_id, "session_id": session_id})
    result_holder = {}

    def _run_render():
        try:
            render_agent = build_agent(tenant_id, quality)
            response = render_agent(prompt)
            result_holder["content"] = str(response)
            result_holder["metrics"] = getattr(response, "metrics", None)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
            result_holder["error"] = exc
        finally:
            app.complete_async_task(task_id)

    worker = threading.Thread(target=_run_render, name="render-worker", daemon=True)
    worker.start()
    # Join rather than fire-and-forget: preserves the existing "one invoke, one response with
    # the finished render" client contract. What add_async_task above changes is that /ping
    # now correctly reports HealthyBusy for this entire wait.
    worker.join()

    if "error" in result_holder:
        exc = result_holder["error"]
        logger.exception(f"Error processing render request: {exc}")
        return {"error": str(exc), "tenant_id": tenant_id, "session_id": session_id}

    content = result_holder.get("content", "")

    usage = {}
    metrics = result_holder.get("metrics")
    if metrics:
        try:
            totals = metrics.accumulated_usage
            usage = {
                "input_tokens": totals.get("inputTokens", 0),
                "output_tokens": totals.get("outputTokens", 0),
            }
        except Exception:
            pass

    logger.info(f"Render complete: {len(content)} chars, usage={usage}")

    return {
        "agent": "3d-render",
        "tenant_id": tenant_id,
        "session_id": session_id,
        "model": BEDROCK_MODEL_ID,
        "quality": quality,
        "response": content,
        "usage": usage,
        "timestamp": datetime.utcnow().isoformat(),
    }


# For local testing
if __name__ == "__main__":
    app.run()
