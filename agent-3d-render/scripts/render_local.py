#!/usr/bin/env python3
"""
Local fixture-driven test harness for blender_runtime.py -- render fixtures with no agent,
Bedrock, or AWS involvement at all. This is the Tier-2 realism plan's inner loop: seconds per
fixture in --quality fast, versus a ~10-minute deploy + GPU spend + a coin-flip on transient EC2
capacity errors to find the same bug in prod.

Usage:
    scripts/render_local.py 'fixtures/edge_*.json' --quality fast --check
    scripts/render_local.py --all --quality full --both --check
    scripts/render_local.py fixtures/edge_hostile_names.json --script-only
    scripts/render_local.py fixtures/edge_legacy_full.json --dry-run

--blender resolution order: --blender flag > $BLENDER_42 env var > .blender/Blender-4.2.0.app
(the pinned-prod-parity install -- see README.md for how to fetch it) > `blender` on PATH, with
a loud warning on that last fallback, since a silent switch to whatever version happens to be
installed locally is exactly how a version-parity bug slips through local testing undetected.
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import blender_runtime  # noqa: E402  (imported to fail fast/loud if the module itself is broken)

_NON_SCENE_KEYS = ("_description", "objects_comment")


def resolve_blender_42(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("BLENDER_42")
    if env:
        return env
    candidates = [
        os.path.join(REPO_ROOT, ".blender", "Blender-4.2.0.app", "Contents", "MacOS", "Blender"),
        os.path.join(REPO_ROOT, ".blender", "blender-4.2.0-linux-x64", "blender"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    which = shutil.which("blender")
    if which:
        print(
            f"WARNING: no pinned Blender 4.2.0 found under .blender/ (see README.md) -- falling "
            f"back to `{which}` on PATH, which may NOT be prod's pinned version. A version-"
            f"parity bug can silently pass here and only surface in prod.",
            file=sys.stderr,
        )
        return which
    raise SystemExit("No Blender found. Install the pinned 4.2.0 (see README.md) or pass --blender.")


def resolve_blender_local():
    """The second binary for --both -- whatever's actually installed locally (e.g. a newer LTS
    via Homebrew), for comparison against the pinned prod version."""
    env = os.environ.get("BLENDER_LOCAL")
    if env and os.path.exists(env):
        return env
    return shutil.which("blender")


QUALITY_OVERRIDES = {
    "fast": {"samples": 24, "resolution_x": 640, "resolution_y": 360},
    "full": {},  # use the fixture's own render_settings unmodified
}


def build_script(scene, output_path):
    """Same JSON-payload handoff as agent.py's _build_blender_script -- duplicated here (not
    imported from agent.py) because agent.py imports boto3/strands/bedrock_agentcore at module
    scope and can't be imported without that stack installed. blender_runtime.py itself has no
    such dependency, which is exactly why it's the file both sides actually share."""
    payload = json.dumps({"scene": scene, "output_path": output_path}, ensure_ascii=True)
    with open(os.path.join(REPO_ROOT, "blender_runtime.py")) as f:
        runtime_source = f.read()
    return f"import json\nPAYLOAD = json.loads({payload!r})\n" + runtime_source


def load_fixture_scene(fixture_path, quality):
    with open(fixture_path) as f:
        scene = json.load(f)
    for key in _NON_SCENE_KEYS:
        scene.pop(key, None)
    overrides = QUALITY_OVERRIDES.get(quality, {})
    if overrides:
        scene = dict(scene)
        scene["render_settings"] = {**scene.get("render_settings", {}), **overrides}

    # Mirror agent.py's _ensure_hdri here, minus S3: the real files are already checked into
    # hdri_assets/ for exactly this -- local testing shouldn't need real AWS credentials to
    # exercise the HDRI code path at all. agent.py's own fetch-from-S3 logic still only gets
    # exercised for real in the final prod deploy.
    env = scene.get("environment") or {}
    if env.get("type") == "HDRI" and env.get("preset") in blender_runtime.HDRI_CATALOG:
        filename = blender_runtime.HDRI_CATALOG[env["preset"]]["filename"]
        local_path = os.path.join(REPO_ROOT, "hdri_assets", filename)
        if os.path.exists(local_path):
            scene = dict(scene)
            scene["environment"] = {**env, "_hdri_local_path": local_path}
        else:
            print(f"WARNING: hdri_assets/{filename} not found -- HDRI will fall back to SKY", file=sys.stderr)

    return scene


def run_fixture(fixture_path, blender_bin, quality, out_dir, dry_run=False, check=False):
    name = os.path.splitext(os.path.basename(fixture_path))[0]
    scene = load_fixture_scene(fixture_path, quality)

    output_path = os.path.join(out_dir, f"{name}.png")
    script = build_script(scene, output_path)

    # Free, instant syntax check before spawning Blender at all -- catches a generator bug in
    # milliseconds instead of paying for a Blender process launch to discover it.
    try:
        compile(script, name, "exec")
    except SyntaxError as exc:
        return {"fixture": name, "status": "compile_error", "error": str(exc)}

    script_path = os.path.join(out_dir, f"{name}_script.py")
    with open(script_path, "w") as f:
        f.write(script)

    env = dict(os.environ)
    if dry_run:
        env["RENDER_DRY_RUN"] = "1"

    t0 = time.time()
    try:
        result = subprocess.run(
            [blender_bin, "--background", "--factory-startup", "--python", script_path],
            capture_output=True, text=True, timeout=600, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"fixture": name, "status": "timeout", "elapsed_seconds": round(time.time() - t0, 2)}
    elapsed = time.time() - t0

    device_info = {"gpu": False, "device_type": None}
    for line in (result.stdout or "").splitlines():
        if line.startswith("RENDER_DEVICE "):
            try:
                device_info = json.loads(line[len("RENDER_DEVICE "):])
            except json.JSONDecodeError:
                pass

    ok = dry_run or os.path.exists(output_path)
    report = {
        "fixture": name,
        "status": "ok" if ok else "render_failed",
        "elapsed_seconds": round(elapsed, 2),
        "device": device_info,
    }
    if not ok:
        report["stderr_tail"] = (result.stderr or "")[-500:]

    if ok and check and not dry_run:
        check_result = subprocess.run(
            [blender_bin, "--background", "--factory-startup", "--python",
             os.path.join(REPO_ROOT, "scripts", "image_checks.py"), "--", output_path],
            capture_output=True, text=True, timeout=120,
        )
        try:
            # image_checks.py's own Blender process prints startup banner lines to stdout before
            # our JSON. json.dumps(..., indent=2) always starts with a line that's exactly "{" --
            # find that (the *first* one, not rindex: the JSON body itself contains nested "{"
            # lines for each check's sub-dict, and rindex would grab the last of those instead of
            # the true top-level opening brace).
            stdout = check_result.stdout
            lines = stdout.splitlines()
            start = next(i for i, line in enumerate(lines) if line.strip() == "{")
            report["image_check"] = json.loads("\n".join(lines[start:]))
        except (StopIteration, ValueError, json.JSONDecodeError):
            report["image_check"] = {"error": "could not parse image_checks.py output", "raw": stdout[-500:]}

    return report


def _fmt_report_line(report, label):
    name = report["fixture"]
    line = f"  {name} [{label}]: {report['status'].upper()} ({report.get('elapsed_seconds', '?')}s)"
    device = report.get("device") or {}
    if device.get("gpu"):
        line += f" device={device['device_type']}"
    ic = report.get("image_check", {})
    if "passed" in ic:
        failing = [
            f"{cname}={c['value']:.4f}"
            for cname, c in ic.get("checks", {}).items()
            if not c["passed"] and not c.get("informational")
        ]
        line += f" checks={'PASS' if ic['passed'] else 'FAIL'}"
        if failing:
            line += " [" + ", ".join(failing) + "]"
    return line


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patterns", nargs="*", help="fixture glob pattern(s), e.g. 'fixtures/edge_*.json'")
    parser.add_argument("--all", action="store_true", help="run every fixture in fixtures/")
    parser.add_argument("--quality", choices=["fast", "full"], default="fast")
    parser.add_argument("--script-only", action="store_true", help="build+compile-check only, no Blender invocation")
    parser.add_argument("--dry-run", action="store_true", help="build the scene inside Blender, skip the actual render")
    parser.add_argument("--both", action="store_true", help="also run under the local (non-pinned) Blender install")
    parser.add_argument("--check", action="store_true", help="run image_checks.py on each successful render")
    parser.add_argument("--blender", default=None, help="explicit Blender 4.2.0 binary path")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, ".render-local-out"))
    args = parser.parse_args()

    patterns = args.patterns or (["fixtures/*.json"] if args.all else [])
    if not patterns:
        parser.error("Provide fixture glob pattern(s), or --all")

    fixtures = sorted({
        p for pattern in patterns
        for p in glob.glob(pattern if os.path.isabs(pattern) else os.path.join(REPO_ROOT, pattern))
        if p.endswith(".json")
    })
    if not fixtures:
        parser.error(f"No fixtures matched: {patterns}")

    os.makedirs(args.out, exist_ok=True)

    if args.script_only:
        n_fail = 0
        for fixture in fixtures:
            name = os.path.basename(fixture)
            scene = load_fixture_scene(fixture, args.quality)
            script = build_script(scene, "/dev/null")
            try:
                compile(script, name, "exec")
                print(f"  {name}: compiles OK ({len(script)} bytes)")
            except SyntaxError as exc:
                print(f"  {name}: COMPILE ERROR: {exc}")
                n_fail += 1
        sys.exit(1 if n_fail else 0)

    binaries = [("4.2.0", resolve_blender_42(args.blender))]
    if args.both:
        local_bin = resolve_blender_local()
        if local_bin:
            binaries.append(("local", local_bin))
        else:
            print("WARNING: --both requested but no local Blender install found on PATH", file=sys.stderr)

    all_reports = []
    for label, binary in binaries:
        print(f"\n=== Blender: {label} ({binary}) ===")
        for fixture in fixtures:
            report = run_fixture(fixture, binary, args.quality, args.out, dry_run=args.dry_run, check=args.check)
            report["blender"] = label
            all_reports.append(report)
            print(_fmt_report_line(report, label))
            if report["status"] not in ("ok",):
                print(f"    {report.get('stderr_tail', report.get('error', ''))[-400:]}")

    out_json = os.path.join(args.out, "report.json")
    with open(out_json, "w") as f:
        json.dump(all_reports, f, indent=2)

    n_fail = sum(
        1 for r in all_reports
        if r["status"] != "ok" or not r.get("image_check", {}).get("passed", True)
    )
    print(f"\n{len(all_reports) - n_fail}/{len(all_reports)} passed. Full report: {out_json}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
