"""
Spike entrypoint for AgentCore's `codeConfiguration` (zip) runtime.

Question this answers: zip agents run as processes directly on the host (per AWS's own docs --
"for directly deployed agents, as processes directly on the instance"), not in a container. If
that's true, there's no container seccomp filter, and Yama's default ptrace_scope=1 allows a
process to ptrace its own descendants unprivileged. zprobe's `-child` mode spawns the traced
program as its own child -- so this might attach without any capability grant at all, unlike the
container path (see ../container-deploy/README.md "Known limitation"), which fails with
`Failed to setup ptrace` because AgentCore's containerConfiguration has no way to grant
CAP_SYS_PTRACE.

Every line that answers one of the spike's numbered questions (see zip-sensor-spike/README.md) is
prefixed "SPIKE:" so 03-run-spike.sh can grep exactly those lines out of CloudWatch.

Fails OPEN, same design as container-deploy/launch-agent.sh and for the same reason: this is
launched as a genuine subprocess, never exec'd directly, so a sensor failure -- including one that
happens after zprobe itself started -- still leaves this process alive to fall back to running the
agent directly.

Two fixes applied after load-testing surfaced a concurrent-cold-start bug (see README.md
"Results" -> the SSM-control comparison): under 10 concurrent first-touch requests against a cold
session, this wrapper caused 18-19 process spawns and every client request timed out, while the
plain (unwrapped) entrypoint handled the identical burst with a single clean bootstrap. Root
cause: zprobe can exit status 0 *without ever having exec'd the agent* (observed directly: pid
2675's sensor subprocess exited 0 at t+1.35s, well before any uvicorn bind), and the old code
treated status==0 as "graceful shutdown, done" unconditionally -- so a premature exit looked
identical to a real shutdown and AgentCore had no signal to do anything but respawn the whole
entrypoint from scratch. Fix 1 (below) makes that distinction explicit by tracking whether the
agent ever actually bound its port. Fix 2 bounds the Secrets Manager call so a slow/contended
credential fetch can't stack up as extra bootstrap latency under concurrent cold starts.
"""

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("zip-sensor-spike")

START_TIME = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
ZPROBE_DIR = os.path.join(HERE, "zprobe-payload")
WORKLOAD_NAME = os.environ.get("ATTRB_WORKLOAD_NAME", "weather-saas-zip")
OTEL_ADDR = os.environ.get("CONFIG_ZPROBE_OTEL_ADDR", "otel-endpoint.app.attrb.io:443")
MEMORY_LIMIT = os.environ.get("ATTRB_MEMORY_LIMIT", "524288000")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def spike_log(question, msg):
    logger.info("SPIKE: [%s] %s", question, msg)


def run_diagnostics():
    """Q1/Q2/Q7 -- answers independent of whether the sensor attaches."""
    spike_log("Q7", f"launcher started at {START_TIME}")
    spike_log("Q1", f"uid={os.getuid()} gid={os.getgid()} pid={os.getpid()}")
    spike_log("Q1", f"dockerenv_present={os.path.exists('/.dockerenv')}")

    for path in ("/proc/1/cgroup", "/proc/self/cgroup"):
        try:
            with open(path) as f:
                lines = f.read().strip().splitlines()[:5]
            spike_log("Q1", f"{path}: {lines}")
        except Exception as e:
            spike_log("Q1", f"{path}: could not read ({e})")

    try:
        with open("/proc/sys/kernel/yama/ptrace_scope") as f:
            spike_log("Q2", f"yama ptrace_scope = {f.read().strip()}")
    except Exception as e:
        spike_log("Q2", f"yama ptrace_scope unreadable ({e}) -- likely means Yama LSM not present")

    spike_log("Q1", f"sys.executable={sys.executable} cwd={os.getcwd()} argv0={sys.argv[0]}")


TOKEN_FETCH_TIMEOUT_SECONDS = 2.5


def fetch_token():
    """Same logic as container-deploy/launch-agent.sh's boto3 fetch -- the secret is
    JSON-wrapped {"token": "<jwt>"}, not a bare token (lesson from the container spike).

    Fix 2: hard-bounded to TOKEN_FETCH_TIMEOUT_SECONDS total. This call sits directly in the
    cold-start path before the agent (sensored or not) can start, so an unbounded or slow
    Secrets Manager call -- more likely under concurrent cold starts contending for the same
    credential-vending endpoint -- becomes extra bootstrap latency stacked on top of everything
    else. Bounding it means the worst case is "start unsensored a bit sooner," which fail-open
    already handles correctly, rather than "block bootstrap indefinitely."
    """
    if os.environ.get("CONFIG_ZPROBE_BEARER_TOKEN"):
        return os.environ["CONFIG_ZPROBE_BEARER_TOKEN"]
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "secretsmanager",
            region_name=AWS_REGION,
            config=Config(
                connect_timeout=TOKEN_FETCH_TIMEOUT_SECONDS,
                read_timeout=TOKEN_FETCH_TIMEOUT_SECONDS,
                retries={"max_attempts": 1},
            ),
        )
        raw = client.get_secret_value(SecretId="attribute/sensor-token")["SecretString"]
        try:
            return json.loads(raw)["token"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return raw
    except Exception as e:
        logger.warning("could not fetch sensor token: %s", e)
        return None


def find_preloader_lib():
    """Port of the vendored _setup_preloader.sh: pick musl vs glibc preloader by checking
    `ldd --version`. Amazon Linux 2023 (the likely AgentCore Instances AMI) is glibc-based."""
    try:
        out = subprocess.run(["ldd", "--version"], capture_output=True, text=True, timeout=5)
        is_musl = "musl" in (out.stdout + out.stderr).lower()
    except Exception as e:
        spike_log("Q5", f"could not run ldd ({e}); assuming glibc")
        is_musl = False

    lib_name = "libpreloader_musl.so" if is_musl else "libpreloader_glibc.so"
    lib_path = os.path.join(ZPROBE_DIR, "lib", lib_name)
    if not os.path.isfile(lib_path):
        spike_log("Q5", f"preloader lib not found at {lib_path}")
        return None
    spike_log("Q1", f"host libc: {'musl' if is_musl else 'glibc'} -> {lib_name}")
    return lib_path


def chmod_executables():
    """Q4: does the zip's unzip step preserve executable bits? chmod defensively regardless."""
    for name in ("zprobe", "seccomp-launcher", "launch/ecs-fargate"):
        path = os.path.join(ZPROBE_DIR, name)
        if os.path.isfile(path):
            was_exec = os.access(path, os.X_OK)
            os.chmod(path, 0o755)
            spike_log("Q4", f"{name}: executable-before-chmod={was_exec}")
        else:
            spike_log("Q4", f"{name}: MISSING from unzipped payload")


AGENT_PORT = 8080
PORT_POLL_INTERVAL_SECONDS = 0.2


def _watch_for_agent_bound(bound_event, stop_event):
    """Fix 1: background thread that detects whether the agent has actually bound its port.

    zprobe can exit status 0 without ever having exec'd the agent (this is the exact bug found
    under concurrent cold-start load -- see the module docstring). Polling here, rather than
    trusting the sensor subprocess's exit code alone, is what lets main() distinguish "the agent
    ran and shut down cleanly" from "nothing ever started" -- both look identical from the exit
    code, but only one of them should skip the unsensored fallback.
    """
    while not stop_event.is_set():
        try:
            with socket.create_connection(("127.0.0.1", AGENT_PORT), timeout=0.5):
                bound_event.set()
                return
        except OSError:
            pass
        stop_event.wait(PORT_POLL_INTERVAL_SECONDS)


def try_start_sensor(agent_cmd):
    """Returns a Popen if the sensor subprocess was launched, else None. Mirrors
    container-deploy/launch-agent.sh's zprobe invocation, run as a genuine subprocess (never
    exec'd) so a post-start sensor failure doesn't take this launcher down with it."""
    token = fetch_token()
    if not token:
        spike_log("Q5", "no sensor token available; skipping sensor")
        return None

    if not os.path.isfile(os.path.join(ZPROBE_DIR, "zprobe")):
        spike_log("Q5", f"zprobe binary not found under {ZPROBE_DIR}; skipping sensor")
        return None

    preloader_lib = find_preloader_lib()
    if not preloader_lib:
        spike_log("Q5", "no usable preloader lib; skipping sensor")
        return None

    env = os.environ.copy()
    env["LD_PRELOAD"] = f"{env.get('LD_PRELOAD', '')}:{preloader_lib}".lstrip(":")

    cmd = [
        os.path.join(ZPROBE_DIR, "zprobe"),
        "-bearer-token", token,
        "-no-otel-collector",
        "-oteladdr", OTEL_ADDR,
        "-probe-engine", "ptrace",
        "-ptrace-target-type", "seccomp-start",
        "-ptrace-trace-child-processes",
        "-workload-name", WORKLOAD_NAME,
        "-memlimit", MEMORY_LIMIT,
        "-child",
    ] + agent_cmd

    spike_log("Q5", f"launching sensor (host process, not exec'd): workload={WORKLOAD_NAME}")
    spike_log("Q7", f"sensor subprocess launched at t+{time.time() - START_TIME:.2f}s")
    try:
        return subprocess.Popen(cmd, env=env)
    except Exception as e:
        spike_log("Q5", f"failed to launch zprobe subprocess: {e}")
        return None


def main():
    run_diagnostics()
    chmod_executables()

    agent_cmd = [sys.executable, os.path.join(HERE, "agent.py")]

    proc = try_start_sensor(agent_cmd)
    if proc is not None:
        # Fix 1: only trust a status==0 exit as "the agent ran and shut down cleanly" if the
        # port-watcher actually saw it bind. Without this, a zprobe process that exits 0 before
        # ever exec'ing the agent (the bug this fixes) looks identical to a real shutdown, and
        # the fallback below never runs -- AgentCore's only remaining option is to respawn this
        # whole entrypoint from scratch, which is what produced 18-19 spawns under concurrent
        # cold-start load.
        bound_event = threading.Event()
        stop_event = threading.Event()
        watcher = threading.Thread(
            target=_watch_for_agent_bound, args=(bound_event, stop_event), daemon=True
        )
        watcher.start()

        status = proc.wait()
        stop_event.set()
        agent_ever_bound = bound_event.is_set()
        spike_log(
            "Q5",
            f"sensor-wrapped process exited with status {status} "
            f"(agent_ever_bound={agent_ever_bound})",
        )

        if status == 0 and agent_ever_bound:
            # The agent actually ran and this is a real graceful shutdown -- propagate, don't
            # restart.
            sys.exit(0)

        spike_log(
            "Q5",
            f"sensor process exited without a confirmed agent run "
            f"(status={status}, agent_ever_bound={agent_ever_bound}); "
            f"starting agent WITHOUT sensor immediately",
        )
    else:
        spike_log("Q5", "starting agent WITHOUT the Attribute sensor (prereqs not met)")

    spike_log("Q7", f"agent starting unsensored at t+{time.time() - START_TIME:.2f}s")
    os.execv(sys.executable, agent_cmd)


if __name__ == "__main__":
    main()
