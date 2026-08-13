#!/usr/bin/env bash
# Invokes the spike runtime once, then greps its CloudWatch logs for the SPIKE: lines and
# prints the Q1-Q7 answer table.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPIKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ ! -f "$SPIKE_DIR/.build/.runtime-arn" ]; then
  echo "ERROR: $SPIKE_DIR/.build/.runtime-arn not found. Run 02-deploy-runtime.sh first." >&2
  exit 1
fi
RUNTIME_ARN="$(cat "$SPIKE_DIR/.build/.runtime-arn")"
RUNTIME_ID="$(echo "$RUNTIME_ARN" | sed -E 's#.*:runtime/##')"
echo "==> Target runtime: $RUNTIME_ARN"

SESSION_ID="spike-session-$(date +%s)-padding-to-reach-the-minimum-length"
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_OUT"' EXIT

echo "==> Invoking (session=$SESSION_ID)..."
# --payload is a blob param; AWS CLI v2 defaults cli-binary-format to base64, which rejects raw
# JSON text -- raw-in-base64-out tells it to accept the raw string and encode it itself.
aws bedrock-agentcore invoke-agent-runtime --region "$REGION" \
  --cli-binary-format raw-in-base64-out \
  --agent-runtime-arn "$RUNTIME_ARN" \
  --runtime-session-id "$SESSION_ID" \
  --payload '{"prompt":"What is the weather in Seattle?","tenant_id":"acme-core"}' \
  "$TMP_OUT" >/dev/null

echo ""
echo "--- agent response ---"
cat "$TMP_OUT"
echo ""

echo ""
echo "==> Waiting 15s for logs to land in CloudWatch..."
sleep 15

LOG_GROUP="/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT"
echo "==> Log group: $LOG_GROUP"
echo ""
echo "=================== SPIKE: answer lines ==================="
aws logs tail "$LOG_GROUP" --region "$REGION" --since 20m 2>&1 | grep "SPIKE:" | sort || \
  echo "(no SPIKE: lines found -- log group may not exist yet, or launcher.py crashed before logging)"
echo "=============================================================="

echo ""
echo "==> Full raw logs (for anything the grep missed):"
aws logs tail "$LOG_GROUP" --region "$REGION" --since 20m 2>&1

echo ""
echo "==> Also check the Attribute dashboard for workload 'weather-saas-zip':"
echo "    https://dashboard.app.attrb.io/services?ue={%22o%22:%22238b64bc-1fe2-4c43-a584-e5b851929cc6%22,%22p%22:%22AWS%22}"
echo "    Reporting lag is expected -- absence immediately after this invocation is not yet a finding."
