# Weather SaaS Agent on AgentCore -- container deployment, with an attempted baked-in sensor

Deploys the same `agent/agent.py` used by the zip-based `weatherSaasAgent` runtime, but as a
**container image** (`containerConfiguration`) on the existing `weatherSaasCapacityProvider`
capacity provider. **This part works and is deployed:** `weatherSaasContainer` is `READY` and every
invocation returns a real Bedrock response.

It also attempts to bake the DoiT Attribute sensor into the same image, so attribution would ship
with the workload instead of depending on the repo's separate host-level EventBridge -> Lambda ->
SSM install path. **This part is currently blocked by the platform, not by anything in this repo** --
see "Known limitation" below. The agent runs unsensored via the fail-open path on every invocation,
confirmed against the real EC2 instance.

The existing zip runtime (`weatherSaasAgent-kHby5SE5ju`) is left running, untouched, for
comparison and instant rollback.

## Why a container needs the sensor baked in, not a sidecar

The [documented Fargate pattern](https://help.doit.com/docs/attribute/integrations/cloud-integrations/aws/attribute-sensor-for-aws-fargate)
runs the sensor as a second container in the task definition that copies its `/app` payload into
a shared volume, then exits; the app container mounts that volume and changes its entrypoint to
the sensor's launcher script. AgentCore's `containerConfiguration` takes a **single `containerUri`**
-- no sidecars, no shared volumes -- so that runtime copy can't happen here.

A multi-stage Docker build reproduces the same end state at *build* time instead:

```dockerfile
FROM quay.io/attribute/sensor:0.0.284 AS sensor
...
COPY --from=sensor /app/ /opt/zprobe/
```

Same files, same `/opt/zprobe` path, just baked in rather than volume-mounted.

`launch/ecs-fargate`, the launcher the public docs point at, is **not actually ECS-specific** --
inspecting the image shows it's a generic `ptrace` + `LD_PRELOAD` wrapper with no dependency on
ECS metadata endpoints. It just doesn't expose `-workload-name` or `-memlimit`, which we want (to
distinguish this in-container sensor from the host-installed one), so `launch-agent.sh` reimplements
its exec line directly against `zprobe`, reusing the vendored `_setup_preloader.sh` helper for
musl/glibc detection rather than duplicating that logic.

## Architecture: x86_64, not ARM64

AgentCore's HTTP protocol contract says ARM64 is required -- but that's for the **microVMs**
compute type. This runtime uses the **Instances** compute type (capacity providers), which
[supports both x86_64 and arm64](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html),
and `weatherSaasCapacityProvider` is `LINUX_X86_64` on `c6i`/`c6a`/`c7i.large` -- Intel/AMD, not
Graviton. **The image must be built `--platform linux/amd64`.** On an Apple Silicon dev machine, a
plain `docker build` produces an arm64 image that will not start on the instance -- this is the
single most likely way to break this deployment, which is why `build-and-push.sh` checks the pushed
manifest's platform and the `Dockerfile`'s `FROM` line pins the platform as a second guard.

## Deploy

```bash
cd container-deploy

# 0. One-time: create the dedicated execution role (Bedrock invoke, CloudWatch Logs,
#    ECR pull, and read access to the attribute/sensor-token secret)
./scripts/00-setup-iam.sh

# 1. Build linux/amd64 (agent + baked-in sensor) and push to ECR
./scripts/01-build-and-push.sh

# 2. Create/update the container agent runtime, attached to weatherSaasCapacityProvider
./scripts/02-deploy-runtime.sh

# 3. Invoke across a few tenants
./scripts/03-smoke-test.sh

# 4. Confirm the sensor actually started (fail-open means a silent skip is possible)
./scripts/04-verify-sensor.sh
```

Re-running `01` + `02` after a code change pushes a new image tag and updates the existing runtime
in place (both scripts are idempotent).

## Sensor configuration

| Concern | How it's handled |
|---|---|
| Placement | In-container only. This runtime does not rely on the repo's EventBridge->Lambda->SSM host install. |
| Token | `launch-agent.sh` reads Secrets Manager (`attribute/sensor-token`) at container start via boto3 (not the AWS CLI, which isn't installed in the image), using the runtime's own execution role -- never stored in the image, runtime config, or git. |
| Failure mode | **Fails open.** `launch-agent.sh` runs the sensor as a genuine subprocess (never `exec`), so if it exits non-zero -- for any reason, including failing *after* it started -- the wrapper is still alive to fall back to starting the agent directly. A sensor issue never fails `/ping` or takes the runtime down. |
| Workload name | `weather-saas-container` -- deliberately distinct from whatever name the host-level installer uses, so the two paths are distinguishable in the Attribute dashboard if both ever fire on the same instance. |

### Known limitation: the sensor does not currently attach on this compute type

Deployed and tested against the real `weatherSaasCapacityProvider` EC2 instance (not just locally),
the sensor consistently fails with `Failed to setup ptrace` (zprobe exit code 42), immediately after
`--config applied`, before it ever launches the agent. This was confirmed on the actual native
x86_64 EC2 instance backing an invoked session -- not a local Docker Desktop/QEMU artifact.

**Root cause:** `zprobe`'s ptrace probe engine needs `CAP_SYS_PTRACE` (its eBPF engine needs
`CAP_SYS_ADMIN`/`CAP_BPF` and unconfined seccomp/AppArmor -- see the equivalent capability list the
k8s DaemonSet chart requires in `helm-chart/operator-chart/templates/_sensor_pod.tpl` in the parent
repo). AgentCore's `create-agent-runtime` API (`containerConfiguration`) has **no field for Linux
capabilities, `privileged` mode, or seccomp/AppArmor overrides** -- confirmed against the full CLI
skeleton, which exposes only `networkConfiguration`, `authorizerConfiguration`,
`requestHeaderConfiguration`, `protocolConfiguration`, `lifecycleConfiguration`,
`environmentVariables`, `filesystemConfigurations`, and `capacityProviderConfiguration`. There is no
documented way, today, to grant an AgentCore container the capability either sensor probe engine
needs.

**What this means in practice:** the agent is fully functional -- every invocation succeeds and
returns a real Bedrock response -- but runs unsensored, via the fail-open path, every time. The
in-container sensor approach as implemented here is **blocked by the platform**, not by a bug in
this Dockerfile or wrapper. If DoiT needs this combination supported, it's a capability-grant gap to
raise with the AgentCore team, not something fixable from this repo alone.

**Possible double-reporting (if the platform gap above is later resolved, or if you fall back to the
host-level path instead):** the existing EventBridge rule in this repo still fires on EC2 instance
launch for this capacity provider, so an instance backing this runtime could get both the
host-installed sensor *and* an in-container one. This deployment doesn't touch that rule.

## Verification

Beyond the numbered scripts:

```bash
# Confirm the pushed image is genuinely amd64
docker buildx imagetools inspect <ECR_URI>:v1 | grep -i platform

# Confirm the sensor payload landed in the image at the expected path
docker run --rm --entrypoint sh <ECR_URI>:v1 -c 'ls /opt/zprobe && ls /opt/zprobe/launch'

# Local service-contract check (runs under emulation on an arm64 Mac -- slow but valid)
docker run --rm -p 8080:8080 --platform linux/amd64 \
  -e AWS_REGION=us-east-1 -v ~/.aws:/root/.aws:ro <ECR_URI>:v1
curl -s localhost:8080/ping
curl -s -XPOST localhost:8080/invocations -H 'Content-Type: application/json' \
  -d '{"prompt":"Weather in Seattle?","tenant_id":"acme-core"}'

# Load generator (reused unchanged -- already sends lowercase x-tenant-id via a boto3
# before-sign handler, which is the real test of the header allowlist path)
AGENT_RUNTIME_ARN=$(cat .runtime-arn) RATE_MULTIPLIER=0.2 \
  python ../scripts/loadgen-agentcore.py
```

## Cost

Each invocation session provisions a real `c6i.large`-class EC2 instance in the account, billed
until `idleRuntimeSessionTimeout` (900s) or `maxLifetime` (8h) -- not free-tier, not serverless.
Leaving the load generator running holds instances up indefinitely.

To stop spend: stop the load generator, or delete active sessions:
```bash
aws bedrock-agentcore-control list-agent-runtime-sessions --region us-east-1 \
  --agent-runtime-id <id from .runtime-arn>
aws bedrock-agentcore-control delete-agent-runtime-session --region us-east-1 \
  --agent-runtime-id <id> --runtime-session-id <session-id>
```

## Rollback

The zip runtime `weatherSaasAgent-kHby5SE5ju` was never touched and is still `READY`. Point
`AGENT_RUNTIME_ARN` back at it to revert traffic instantly.

To remove this container runtime entirely:
```bash
aws bedrock-agentcore-control delete-agent-runtime --region us-east-1 \
  --agent-runtime-id <id from .runtime-arn>
aws ecr delete-repository --repository-name bedrock-agentcore-weather-saas-container \
  --region us-east-1 --force
aws iam delete-role-policy --role-name WeatherSaasContainerRuntimeRole \
  --policy-name WeatherSaasContainerExecutionPolicy
aws iam delete-role --role-name WeatherSaasContainerRuntimeRole
```

## Out of scope

- No changes to `agent/agent.py`, the existing Terraform, its state, or the zip runtime.
- No changes to the existing EventBridge->Lambda->SSM host-install path (see the
  double-reporting note above).
- No GPU instance types, persistent EBS volume mounts, custom JWT auth, or streaming/SSE responses.
- No Java `jagent.jar` wiring -- this is a Python workload; `JAVA_TOOL_OPTIONS` from the Fargate
  doc doesn't apply.
