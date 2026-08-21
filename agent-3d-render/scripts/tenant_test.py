#!/usr/bin/env python3
"""
Two-tenant, multi-request smoke test for the render3d agent runtime.

Sends several render prompts per tenant against the same agent runtime, using a real
x-tenant-id HTTP header (not just a payload field) so this exercises the actual header-based
tenant-routing path end-to-end. boto3's invoke_agent_runtime has no first-class parameter for
custom headers, so this registers a botocore "before-sign" event handler that injects the
header just before each request is signed.

Usage:
    python3 -m venv .venv && .venv/bin/pip install boto3   # if boto3 isn't already available
    AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-west-2:ACCOUNT:runtime/NAME \
        .venv/bin/python3 scripts/tenant_test.py

Environment variables:
    AGENT_RUNTIME_ARN: ARN of the agent runtime to invoke (required; no default -- the runtime
        ID changes any time the capacity provider or runtime is recreated, e.g. after a
        terraform destroy/apply cycle, so hardcoding one here would silently go stale)
    AWS_REGION: defaults to us-west-2
    RESULTS_FILE: where to write full JSON results (default: results next to this script)

After a successful run, download the rendered images with:
    aws s3 sync s3://<your-output-bucket>/render3d-agent/outputs/<tenant>/ ./renders/<tenant>/
"""
import json
import os
import threading
import time
import uuid

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-west-2")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN")
RESULTS_FILE = os.environ.get("RESULTS_FILE", os.path.join(os.path.dirname(__file__), "tenant_test_results.json"))
TENANT_ID_HEADER = "x-tenant-id"

if not AGENT_RUNTIME_ARN:
    raise SystemExit(
        "Set AGENT_RUNTIME_ARN to the runtime you want to test, e.g.:\n"
        "  aws bedrock-agentcore-control list-agent-runtimes --region us-west-2 "
        "--query \"agentRuntimes[?contains(agentRuntimeName,'render3d')]\"\n"
        "then: AGENT_RUNTIME_ARN=<arn> python3 scripts/tenant_test.py"
    )

_thread_local = threading.local()


def add_tenant_header(request, **kwargs):
    """botocore before-sign handler: attaches the current thread's tenant_id as a real header."""
    tenant_id = getattr(_thread_local, "tenant_id", None)
    if tenant_id:
        request.headers.add_header(TENANT_ID_HEADER, tenant_id)


config = Config(read_timeout=280, connect_timeout=30, retries={"max_attempts": 1})
client = boto3.client("bedrock-agentcore", region_name=REGION, config=config)
client.meta.events.register("before-sign.bedrock-agentcore.InvokeAgentRuntime", add_tenant_header)


TENANTS = {
    "acme-architects": [
        "Design a futuristic glass skyscraper at sunset with dramatic orange lighting, draft quality",
        "A luxury sports car showroom with reflective marble floors and spotlights, draft quality",
    ],
    "globex-gamestudio": [
        "A fantasy castle on a cliff with dragons flying overhead in stormy weather, draft quality",
        "A cyberpunk city street at night with neon signs and rain reflections, draft quality",
    ],
}

results = []

for tenant_id, prompts in TENANTS.items():
    session_id = f"{tenant_id}-session-{uuid.uuid4().hex}-abcdefghij"
    print(f"\n=== TENANT: {tenant_id}  (session: {session_id}) ===")
    _thread_local.tenant_id = tenant_id

    for i, prompt in enumerate(prompts, 1):
        print(f"\n--- request {i}/{len(prompts)}: {prompt[:70]}...")
        payload = json.dumps({"prompt": prompt, "quality": "draft"}).encode()
        t0 = time.time()
        try:
            resp = client.invoke_agent_runtime(
                agentRuntimeArn=AGENT_RUNTIME_ARN,
                runtimeSessionId=session_id,
                qualifier="DEFAULT",
                payload=payload,
                contentType="application/json",
            )
            body = resp["response"].read()
            elapsed = time.time() - t0
            parsed = json.loads(body)
            print(f"OK ({elapsed:.1f}s) tenant_id_echoed={parsed.get('tenant_id')}")
            print(f"response: {parsed.get('response', '')[:300]}")
            results.append({"tenant": tenant_id, "prompt": prompt, "elapsed": elapsed, "ok": True, "parsed": parsed})
        except Exception as exc:
            elapsed = time.time() - t0
            print(f"FAILED ({elapsed:.1f}s): {exc}")
            results.append({"tenant": tenant_id, "prompt": prompt, "elapsed": elapsed, "ok": False, "error": str(exc)})

print("\n\n=== SUMMARY ===")
for r in results:
    status = "OK" if r["ok"] else "FAILED"
    print(f"[{status}] {r['tenant']}: {r['prompt'][:50]}... ({r['elapsed']:.1f}s)")

with open(RESULTS_FILE, "w") as f:
    json.dump(results, f, indent=2, default=str)
print(f"\nFull results written to {RESULTS_FILE}")
