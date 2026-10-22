"""Turn Worker Versioning on (or off) for the deployed AgentCore runtime, in place.

Serverless Workers need the ephemeral Worker to poll as a specific Worker Deployment
Version, which the runtime does when TEMPORAL_DEPLOYMENT_NAME and TEMPORAL_BUILD_ID are
in its container env. This flips those on the live runtime via UpdateAgentRuntime, with
no image rebuild: it reads the current config, merges the env, and writes it back, then
waits for READY. `--clear` removes them to restore the unversioned (manual-invoke) state.

Usage:
  RUNTIME_ID=<your runtime id> DEPLOYMENT_NAME=durable-agent-demo BUILD_ID=v1 \
    uv run python deploy/agentcore/set_runtime_versioning.py
  RUNTIME_ID=<your runtime id> uv run python deploy/agentcore/set_runtime_versioning.py --clear

Required env: RUNTIME_ID (your AgentCore runtime id). AWS_REGION defaults to us-east-1.
"""

from __future__ import annotations

import os
import sys
import time

import boto3

RUNTIME_ID = os.environ.get("RUNTIME_ID", "")
VERSION_VARS = ("TEMPORAL_DEPLOYMENT_NAME", "TEMPORAL_BUILD_ID")


def _wait_ready(control, timeout: float = 300.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = control.get_agent_runtime(agentRuntimeId=RUNTIME_ID)["status"]
        if status == "READY":
            return status
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETING"):
            raise SystemExit(f"runtime entered {status}")
        time.sleep(5)
    raise SystemExit("timed out waiting for runtime to become READY")


def main() -> None:
    if not RUNTIME_ID:
        raise SystemExit("set RUNTIME_ID to your AgentCore runtime id")
    clear = "--clear" in sys.argv[1:]
    region = os.environ.get("AWS_REGION", "us-east-1")
    control = boto3.client("bedrock-agentcore-control", region_name=region)

    current = control.get_agent_runtime(agentRuntimeId=RUNTIME_ID)
    env = dict(current.get("environmentVariables", {}))

    if clear:
        for var in VERSION_VARS:
            env.pop(var, None)
        action = "cleared versioning env (unversioned; manual-invoke only)"
    else:
        name = os.environ.get("DEPLOYMENT_NAME", "durable-agent-demo")
        build_id = os.environ.get("BUILD_ID", "v1")
        env["TEMPORAL_DEPLOYMENT_NAME"] = name
        env["TEMPORAL_BUILD_ID"] = build_id
        if os.environ.get("AGENTCORE_DEBOUNCE_SECONDS"):
            env["AGENTCORE_DEBOUNCE_SECONDS"] = os.environ["AGENTCORE_DEBOUNCE_SECONDS"]
        action = f"set versioning env deployment={name} build={build_id}"

    control.update_agent_runtime(
        agentRuntimeId=RUNTIME_ID,
        agentRuntimeArtifact=current["agentRuntimeArtifact"],
        roleArn=current["roleArn"],
        networkConfiguration=current.get("networkConfiguration", {"networkMode": "PUBLIC"}),
        lifecycleConfiguration=current.get("lifecycleConfiguration", {}),
        environmentVariables=env,
    )
    print(f"update submitted: {action}", flush=True)
    _wait_ready(control)
    after = control.get_agent_runtime(agentRuntimeId=RUNTIME_ID)
    keys = sorted(after.get("environmentVariables", {}).keys())
    print(f"runtime READY (version {after['agentRuntimeVersion']}); env keys: {keys}", flush=True)


if __name__ == "__main__":
    main()
