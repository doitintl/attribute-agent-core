#!/usr/bin/env bash
# Removes the spike runtime and its S3 artifact. Does not touch the capacity provider, the
# reused IAM role, weatherSaasAgent (zip), weatherSaasContainer, or anything under container-deploy.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPIKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RUNTIME_ID="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='weatherSaasZipSensor'].agentRuntimeId" \
  --output text)"

if [ -n "$RUNTIME_ID" ]; then
  echo "==> Deleting runtime $RUNTIME_ID..."
  aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" \
    --agent-runtime-id "$RUNTIME_ID"
else
  echo "==> No weatherSaasZipSensor runtime found; nothing to delete."
fi

if [ -f "$SPIKE_DIR/.build/.bucket" ] && [ -f "$SPIKE_DIR/.build/.key" ]; then
  BUCKET="$(cat "$SPIKE_DIR/.build/.bucket")"
  KEY="$(cat "$SPIKE_DIR/.build/.key")"
  echo "==> Deleting s3://$BUCKET/$KEY..."
  aws s3 rm "s3://$BUCKET/$KEY" --region "$REGION" || true
fi

echo "==> Done. Capacity provider, IAM role, and other runtimes were left untouched."
