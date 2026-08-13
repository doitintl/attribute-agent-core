# Spike: Attribute sensor bundled inside the AgentCore zip artifact

Proves out an alternative way to get the DoiT Attribute sensor onto an AgentCore zip
(`codeConfiguration`) runtime: bake `zprobe` directly into the deployment zip and launch it
alongside the agent, instead of installing it after the fact via the existing
EventBridge -> Lambda -> SSM path.

## Why

Two problems with the current install-after-the-fact approach:

1. **Egress cost.** Every new instance downloads the ~107 MB `zprobe` binary from
   `storage.googleapis.com` -- cross-cloud GCS egress billed to DoiT, on every cold start.
2. **Startup race.** The sensor installs *after* the instance is already up and the agent may
   already be running, so there's a window where traffic isn't attributed.

Bundling the sensor in the same S3 zip the agent already deploys from fixes both: same-region
S3->EC2 transfer is free, and the sensor is in place before the agent's first instruction.

## Fargate-based, not EC2-based

This deliberately follows the **Fargate sidecar** approach
([DoiT Fargate docs](https://help.doit.com/docs/attribute/integrations/cloud-integrations/aws/attribute-sensor-for-aws-fargate)),
not the [standard EC2 install](https://help.doit.com/docs/attribute/integrations/cloud-integrations/aws/attribute-ec2-sensor-installation).
The EC2 path uses **eBPF** and requires `sudo`/root plus a systemd service -- none of which are
available here, since AgentCore's zip entrypoint runs as an unprivileged, non-root process with no
sudo. Fargate solves the same "no root, no eBPF" constraint with `zprobe`'s **ptrace** probe engine
instead, launched via the vendored `launch/ecs-fargate` script. `launcher.py` in this directory
runs the same ptrace-based approach directly in-process, relying on the host's default Yama
ptrace policy (`ptrace_scope=0`) to allow unprivileged tracing of a child process.

## What was proven

- The sensor attaches successfully and traces the agent with **no elevated privileges** -- zero
  ptrace-related failures across all testing.
- Sensor payload, token fetch, and agent startup all happen inside the existing zip deploy, with
  no additional infrastructure.
- Startup order is deterministic: sensor is running before the agent's first request, not racing
  it.
- Fails open by design: if the sensor can't start for any reason, the agent starts anyway
  unaffected.

## Known issue: concurrent cold start

Under a burst of concurrent requests hitting a brand-new session at once, AgentCore's own
bootstrap retries interacted badly with the sensor wrapper: the sensor process could exit cleanly
*before the agent had even started*, and the old code treated that as "done" instead of "try
again" -- so AgentCore just kept respawning the whole entrypoint from scratch instead of
recovering, and every request in the burst timed out.

Fixed by checking whether the agent actually came up before trusting a clean sensor exit, plus a
hard timeout on the token fetch so a slow credential lookup can't stack up as extra startup delay.
After the fix, the same concurrent burst succeeded every time in testing. Caveat: AgentCore still
launches multiple entrypoint attempts under load -- the fix makes each one recover instantly
instead of failing, it doesn't stop AgentCore from retrying in the first place.

## Run it

```bash
cd zip-sensor-spike

./scripts/01-build-zip.sh       # bundle sensor payload + agent + deps, upload to S3
./scripts/02-deploy-runtime.sh  # create/update the weatherSaasZipSensor runtime
./scripts/03-run-spike.sh       # invoke once, check logs for sensor status
./scripts/99-cleanup.sh         # tear down when done
```

## Status

Spike complete -- sensor-bundling-via-ptrace is viable on this compute type. Not productized:
