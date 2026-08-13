#!/usr/bin/env bash
# Creates (or updates) the zip-based spike runtime, attached to the same capacity provider used
# by container-deploy. Idempotent, same create-or-update + poll-READY shape as
# ../../container-deploy/scripts/02-deploy-runtime.sh.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
RUNTIME_NAME="weatherSaasZipSensor"
CAPACITY_PROVIDER_NAME="${CAPACITY_PROVIDER_NAME:-weatherSaasCapacityProvider}"
# Reused as-is: already has Bedrock invoke + CloudWatch Logs + secretsmanager:GetSecretValue on
# attribute/sensor-token-* -- exactly what this spike runtime needs. A dedicated role is a
# productization concern, out of scope for a spike.
ROLE_ARN="${ROLE_ARN:-arn:aws:iam::058264544288:role/WeatherSaasContainerRuntimeRole}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPIKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ ! -f "$SPIKE_DIR/.build/.bucket" ] || [ ! -f "$SPIKE_DIR/.build/.key" ]; then
  echo "ERROR: $SPIKE_DIR/.build/.bucket or .key not found. Run 01-build-zip.sh first." >&2
  exit 1
fi
BUCKET="$(cat "$SPIKE_DIR/.build/.bucket")"
KEY="$(cat "$SPIKE_DIR/.build/.key")"

echo "==> Artifact: s3://$BUCKET/$KEY"
echo "==> Execution role ARN: $ROLE_ARN"

echo "==> Looking up capacity provider '$CAPACITY_PROVIDER_NAME'..."
CP_ARN="$(aws bedrock-agentcore-control list-capacity-providers --region "$REGION" \
  --query "capacityProviders[?name=='$CAPACITY_PROVIDER_NAME'].capacityProviderArn" \
  --output text)"
if [ -z "$CP_ARN" ]; then
  echo "ERROR: capacity provider '$CAPACITY_PROVIDER_NAME' not found in $REGION." >&2
  exit 1
fi
echo "==> Capacity provider ARN: $CP_ARN"

PAYLOAD_FILE="$(mktemp)"
trap 'rm -f "$PAYLOAD_FILE"' EXIT

cat > "$PAYLOAD_FILE" <<JSON
{
  "agentRuntimeName": "$RUNTIME_NAME",
  "agentRuntimeArtifact": {
    "codeConfiguration": {
      "code": {
        "s3": {
          "bucket": "$BUCKET",
          "prefix": "$KEY"
        }
      },
      "runtime": "PYTHON_3_13",
      "entryPoint": ["launcher.py"]
    }
  },
  "roleArn": "$ROLE_ARN",
  "capacityProviderConfiguration": {
    "capacityProviderArn": "$CP_ARN"
  },
  "requestHeaderConfiguration": {
    "requestHeaderAllowlist": ["x-tenant-id"]
  },
  "lifecycleConfiguration": {
    "idleRuntimeSessionTimeout": 900,
    "maxLifetime": 28800
  },
  "environmentVariables": {
    "ATTRB_WORKLOAD_NAME": "weather-saas-zip",
    "CONFIG_ZPROBE_OTEL_ADDR": "otel-endpoint.app.attrb.io:443",
    "ATTRB_MEMORY_LIMIT": "524288000",
    "AWS_REGION": "$REGION"
  }
}
JSON

echo "==> Checking for an existing runtime named '$RUNTIME_NAME'..."
EXISTING_ID="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId" \
  --output text)"

if [ -n "$EXISTING_ID" ]; then
  echo "==> Runtime exists ($EXISTING_ID); updating..."
  python3 -c "
import json
with open('$PAYLOAD_FILE') as f:
    d = json.load(f)
d.pop('agentRuntimeName', None)
d['agentRuntimeId'] = '$EXISTING_ID'
print(json.dumps(d))
" > "$PAYLOAD_FILE.update"
  aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
    --cli-input-json "file://$PAYLOAD_FILE.update" >/dev/null
  RUNTIME_ID="$EXISTING_ID"
  rm -f "$PAYLOAD_FILE.update"
else
  echo "==> Creating new runtime '$RUNTIME_NAME'..."
  RUNTIME_ID="$(aws bedrock-agentcore-control create-agent-runtime --region "$REGION" \
    --cli-input-json "file://$PAYLOAD_FILE" \
    --query 'agentRuntimeId' --output text)"
fi

echo "==> Runtime ID: $RUNTIME_ID"
echo "==> Waiting for runtime to reach READY..."
for i in $(seq 1 30); do
  STATUS="$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
    --agent-runtime-id "$RUNTIME_ID" --query 'status' --output text)"
  echo "    [$i/30] status=$STATUS"
  if [ "$STATUS" = "READY" ]; then
    break
  fi
  if [ "$STATUS" = "CREATE_FAILED" ] || [ "$STATUS" = "UPDATE_FAILED" ]; then
    echo "ERROR: runtime reached $STATUS" >&2
    aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
      --agent-runtime-id "$RUNTIME_ID" >&2 || true
    exit 1
  fi
  sleep 10
done

if [ "$STATUS" != "READY" ]; then
  echo "ERROR: timed out waiting for READY (last status: $STATUS)" >&2
  exit 1
fi

RUNTIME_ARN="$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
  --agent-runtime-id "$RUNTIME_ID" --query 'agentRuntimeArn' --output text)"
echo "$RUNTIME_ARN" > "$SPIKE_DIR/.build/.runtime-arn"
echo "==> READY. Runtime ARN: $RUNTIME_ARN"
