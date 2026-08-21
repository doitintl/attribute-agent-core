#!/usr/bin/env python3
"""
Probe a Blender install for exactly which bpy properties/enum-values/sockets this version
supports, and print a JSON report. Run under BOTH the pinned prod version (4.2.0) and whatever
version is on the local dev machine, then diff -- this converts the whole "which Blender version
has which API" question into a one-time lookup table, done *before* writing feature code that
depends on the answer (per the Tier-2 realism plan's Phase 1).

Why probe instead of checking bpy.app.version: Blender's enum *introspection* is unreliable
headless -- e.g. `scene.view_settings.view_transform... .enum_items` can report something
useless like ['NONE'] under `--background` even though direct assignment of 'Standard' / 'AgX' /
'Khronos PBR Neutral' all succeed. So every candidate here is tested by actually assigning it to
a real, live datablock and checking whether it stuck -- the same try/except-assignment pattern
blender_runtime.py's _sattr/_senum/_sock use at render time.

Usage:
    blender --background --factory-startup --python scripts/capability_probe.py
    # or, to compare two installs:
    blender --background --factory-startup --python scripts/capability_probe.py -- --out /tmp/probe_52.json
    .blender/Blender-4.2.0.app/Contents/MacOS/Blender --background --factory-startup \
        --python scripts/capability_probe.py -- --out /tmp/probe_42.json
    diff <(python3 -m json.tool /tmp/probe_42.json) <(python3 -m json.tool /tmp/probe_52.json)
"""
import json
import sys

try:
    import bpy
except ImportError:
    print("This script must be run inside Blender: blender --background --python capability_probe.py", file=sys.stderr)
    sys.exit(1)


def _sattr(obj, name, value):
    try:
        setattr(obj, name, value)
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def _senum(obj, name, candidates):
    """Returns the list of candidates that stuck (not just the first -- for probing we want to
    see every value this version accepts, not stop at the first hit)."""
    accepted = []
    for value in candidates:
        if _sattr(obj, name, value):
            accepted.append(value)
    return accepted


def probe_view_transform(report):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    vs = bpy.context.scene.view_settings
    report["view_transform"] = _senum(vs, "view_transform", [
        "Standard", "AgX", "Khronos PBR Neutral", "Filmic", "Filmic Log", "False Color", "Raw",
    ])
    report["look"] = {}
    for vt in report["view_transform"]:
        _sattr(vs, "view_transform", vt)
        report["look"][vt] = _senum(vs, "look", [
            "None", "Very Low Contrast", "Low Contrast", "Medium Low Contrast",
            "Medium Contrast", "Medium High Contrast", "High Contrast", "Very High Contrast",
        ])


def probe_sky(report):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    world = bpy.data.worlds.new("ProbeWorld")
    world.use_nodes = True
    node_tree = world.node_tree
    sky = node_tree.nodes.new("ShaderNodeTexSky")
    report["sky_type"] = _senum(sky, "sky_type", [
        "NISHITA", "MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "PREETHAM", "HOSEK_WILKIE",
    ])
    # dust_density (4.2) was renamed aerosol_density (5.x) -- probe both, whichever exists wins.
    for sky_type in report["sky_type"]:
        _sattr(sky, "sky_type", sky_type)
        if sky_type in ("NISHITA", "MULTIPLE_SCATTERING", "SINGLE_SCATTERING"):
            report.setdefault("dust_or_aerosol_density", {})[sky_type] = {
                "dust_density": _sattr(sky, "dust_density", 1.0),
                "aerosol_density": _sattr(sky, "aerosol_density", 1.0),
            }
    report["sky_sockets"] = [s.name for s in sky.outputs] + [s.name for s in sky.inputs]


def probe_principled_bsdf(report):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mat = bpy.data.materials.new("ProbeMat")
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    report["principled_bsdf_inputs"] = [s.name for s in bsdf.inputs]
    report["principled_bsdf_outputs"] = [s.name for s in bsdf.outputs]

    def try_set(candidates, value):
        for name in candidates:
            socket = bsdf.inputs.get(name)
            if socket is None:
                continue
            try:
                socket.default_value = value
                return name
            except (TypeError, ValueError):
                continue
        return None

    report["socket_aliases"] = {
        "specular": try_set(["Specular IOR Level", "Specular"], 0.5),
        "transmission": try_set(["Transmission Weight", "Transmission"], 0.5),
        "emission_color": try_set(["Emission Color", "Emission"], (1, 1, 1, 1)),
        "emission_strength": try_set(["Emission Strength"], 1.0),
        "coat_weight": try_set(["Coat Weight", "Clearcoat"], 0.5),
        "ior": try_set(["IOR"], 1.45),
        "alpha": try_set(["Alpha"], 1.0),
    }
    report["thin_wall_present"] = "Thin Wall" in report["principled_bsdf_inputs"]


def probe_texture_nodes(report):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mat = bpy.data.materials.new("ProbeMat2")
    mat.use_nodes = True
    nt = mat.node_tree

    noise = nt.nodes.new("ShaderNodeTexNoise")
    report["noise_sockets"] = [s.name for s in noise.inputs]

    bump = nt.nodes.new("ShaderNodeBump")
    report["bump_sockets"] = [s.name for s in bump.inputs]

    brick = nt.nodes.new("ShaderNodeTexBrick")
    report["brick_sockets"] = [s.name for s in brick.inputs] + [s.name for s in brick.outputs]

    voronoi = nt.nodes.new("ShaderNodeTexVoronoi")
    report["voronoi_sockets"] = [s.name for s in voronoi.inputs]

    colorramp = nt.nodes.new("ShaderNodeValToRGB")
    report["colorramp_has_constant_interp"] = _sattr(colorramp.color_ramp, "interpolation", "CONSTANT")


def probe_geometry(report):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.active_object
    obj.scale = (2.0, 1.0, 0.5)
    report["transform_apply_scale"] = True
    try:
        bpy.ops.object.transform_apply(scale=True)
    except Exception as exc:
        report["transform_apply_scale"] = f"FAILED: {exc}"

    report["has_shade_smooth_by_angle"] = hasattr(bpy.ops.object, "shade_smooth_by_angle")
    report["has_use_auto_smooth"] = hasattr(obj.data, "use_auto_smooth")  # removed in 4.1+

    bevel_mod = obj.modifiers.new(name="ProbeBevel", type="BEVEL")
    report["bevel_modifier_props"] = [p.identifier for p in bevel_mod.bl_rna.properties if not p.is_readonly]

    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24)
    report["uv_sphere_tessellation_args"] = True

    light_data = bpy.data.lights.new(name="ProbeArea", type="AREA")
    report["area_light_has_size"] = hasattr(light_data, "size")
    sun_data = bpy.data.lights.new(name="ProbeSun", type="SUN")
    report["sun_light_has_angle"] = hasattr(sun_data, "angle")
    spot_data = bpy.data.lights.new(name="ProbeSpot", type="SPOT")
    report["spot_light_props"] = [p for p in ("spot_size", "spot_blend", "shadow_soft_size") if hasattr(spot_data, p)]


def probe_numpy(report):
    try:
        import numpy
        report["numpy_version"] = numpy.__version__
        report["numpy_available"] = True
    except ImportError:
        report["numpy_available"] = False
        report["numpy_version"] = None


def probe_render_devices(report):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    accepted_device_types = []
    for device_type in ["OPTIX", "METAL", "HIP", "CUDA", "ONEAPI"]:
        if _sattr(prefs, "compute_device_type", device_type):
            accepted_device_types.append(device_type)
    report["compute_device_types_accepted"] = accepted_device_types
    report["denoiser"] = _senum(scene.cycles, "denoiser", ["OPTIX", "OPENIMAGEDENOISE", "NLM"])


def main():
    report = {"blender_version": list(bpy.app.version), "blender_version_string": bpy.app.version_string}
    probe_view_transform(report)
    probe_sky(report)
    probe_principled_bsdf(report)
    probe_texture_nodes(report)
    probe_geometry(report)
    probe_render_devices(report)
    probe_numpy(report)

    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    out_path = None
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]

    text = json.dumps(report, indent=2, default=str)
    if out_path:
        with open(out_path, "w") as f:
            f.write(text)
        print(f"Capability report written to {out_path}")
    else:
        print(text)


main()
