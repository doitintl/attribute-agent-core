#!/usr/bin/env bash
# Assembles the spike's zip artifact: agent code + deps (reused from the existing zip's
# packaging convention -- flat pip install -t ., agent.py at root) + launcher.py + the
# Attribute sensor payload extracted from the container image, and uploads it to S3.
#
# Dependencies are installed inside a linux/amd64 container (not cross-platform pip resolution
# on the dev Mac's arm64 host) because strands-agents needs `git` to install one of its
# dependencies (same requirement hit building container-deploy/Dockerfile) -- cross-platform
# --only-binary wheel resolution can't satisfy that, but a native-arch container install can.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPIKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SPIKE_DIR/.." && pwd)"

REGION="${AWS_REGION:-us-east-1}"
BUCKET="${ARTIFACT_BUCKET:-doit-agentcore-artifacts}"
PREFIX="${ARTIFACT_PREFIX:-weather-saas-zip-sensor}"
SENSOR_IMAGE="${SENSOR_IMAGE:-quay.io/attribute/sensor:0.0.284}"

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
echo "==> Build dir: $BUILD_DIR"

echo "==> Extracting Attribute sensor payload from $SENSOR_IMAGE..."
docker pull --platform linux/amd64 "$SENSOR_IMAGE" >/dev/null
CID="$(docker create --platform linux/amd64 --entrypoint /bin/true "$SENSOR_IMAGE")"
docker cp "$CID:/app" "$BUILD_DIR/zprobe-payload"
docker rm "$CID" >/dev/null
echo "    payload size: $(du -sh "$BUILD_DIR/zprobe-payload" | cut -f1)"

echo "==> Installing agent dependencies (linux/amd64 container, flat layout matching the existing zip)..."
# NOTE: installs to a path INSIDE the container's own filesystem, then docker cp's it out --
# NOT a bind-mounted volume. A bind mount here silently loses all written files: pip reports
# "Successfully installed ..." but the host-side mount point ends up empty. Root-caused to
# colima's QEMU emulation of --platform linux/amd64 on this arm64 Mac not syncing bind-mount
# writes back reliably from an emulated-arch container -- docker cp (which goes through
# Docker's own container-filesystem copy, not the live bind-mount sync path) does not have
# this problem, matching the sensor-payload extraction above.
docker rm -f zip-spike-pipbuild >/dev/null 2>&1 || true
docker create --platform linux/amd64 --name zip-spike-pipbuild \
  -v "$REPO_ROOT/agent/requirements.txt:/req.txt:ro" \
  public.ecr.aws/docker/library/python:3.13-slim \
  sh -c "apt-get update -qq && apt-get install -y -qq --no-install-recommends git >/dev/null && \
         pip install --no-cache-dir -t /out -r /req.txt --quiet" >/dev/null
docker start -a zip-spike-pipbuild
docker cp zip-spike-pipbuild:/out "$BUILD_DIR/deps"
docker rm zip-spike-pipbuild >/dev/null

echo "==> Copying agent code + launcher..."
cp -r "$BUILD_DIR/deps/." "$BUILD_DIR/"
rm -rf "$BUILD_DIR/deps"
cp "$REPO_ROOT/agent/agent.py" "$BUILD_DIR/agent.py"
cp "$SPIKE_DIR/launcher.py" "$BUILD_DIR/launcher.py"

echo "==> Zipping..."
ZIP_PATH="$SPIKE_DIR/.build/weather-saas-zip-sensor.zip"
mkdir -p "$(dirname "$ZIP_PATH")"
rm -f "$ZIP_PATH"
(cd "$BUILD_DIR" && zip -rq "$ZIP_PATH" . -x '*.pyc' -x '*__pycache__*')

SIZE_MB=$(( $(stat -f%z "$ZIP_PATH" 2>/dev/null || stat -c%s "$ZIP_PATH") / 1024 / 1024 ))
echo "==> Zip built: $ZIP_PATH (${SIZE_MB} MiB)"
if [ "$SIZE_MB" -gt 250 ]; then
  echo "    NOTE: this is a spike finding (Q3) -- record the size limit if create-agent-runtime rejects it." >&2
fi

echo "==> Uploading to s3://$BUCKET/$PREFIX/weather-saas-zip-sensor.zip..."
aws s3 cp "$ZIP_PATH" "s3://$BUCKET/$PREFIX/weather-saas-zip-sensor.zip" --region "$REGION"

echo "$BUCKET" > "$SPIKE_DIR/.build/.bucket"
echo "$PREFIX/weather-saas-zip-sensor.zip" > "$SPIKE_DIR/.build/.key"
echo "==> Done."
