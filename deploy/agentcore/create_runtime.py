"""Create the AgentCore runtime for the demo (boto3 CreateAgentRuntime).

Needs an AWS account with Bedrock and AgentCore access. PUBLIC mode is deliberate: it is the
mode that permits the container's outbound gRPC to Temporal Cloud at
<region>.aws.api.temporal.io:7233 with no VPC or NAT.

Required environment:
  AWS_REGION, EXECUTION_ROLE_ARN, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE,
  TEMPORAL_API_KEY. Optional: IMAGE_URI, MODEL_MODE (default bedrock),
  BEDROCK_MODEL_ID.
"""

from __future__ import annotations

import os

import boto3


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def main() -> None:
    region = _require("AWS_REGION")
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    image = os.environ.get(
        "IMAGE_URI", f"{account}.dkr.ecr.{region}.amazonaws.com/durable-agent-demo:latest"
    )
    role = _require("EXECUTION_ROLE_ARN")

    container_env = {
        "MODEL_MODE": os.environ.get("MODEL_MODE", "bedrock"),
        "AWS_REGION": region,
        "TEMPORAL_TARGET": "cloud",
        "TEMPORAL_ADDRESS": _require("TEMPORAL_ADDRESS"),
        "TEMPORAL_NAMESPACE": _require("TEMPORAL_NAMESPACE"),
        "TEMPORAL_API_KEY": _require("TEMPORAL_API_KEY"),
    }
    if os.environ.get("BEDROCK_MODEL_ID"):
        container_env["BEDROCK_MODEL_ID"] = os.environ["BEDROCK_MODEL_ID"]

    # Worker Versioning turns the ephemeral Worker into a Serverless Worker: it polls
    # as a specific Worker Deployment Version, which is what lets Temporal's Worker
    # Controller invoke this runtime on demand. Set both to enable it; leave unset to
    # keep the runtime unversioned (manual-invoke only). See serverless.sh.
    for var in ("TEMPORAL_DEPLOYMENT_NAME", "TEMPORAL_BUILD_ID", "AGENTCORE_DEBOUNCE_SECONDS"):
        if os.environ.get(var):
            container_env[var] = os.environ[var]

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    resp = control.create_agent_runtime(
        agentRuntimeName="durable_agent_demo",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=role,
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        # verify: the exact field for container environment variables against the
        # current CreateAgentRuntime API. If it is not accepted here, inject
        # TEMPORAL_* and MODEL_MODE via Secrets Manager or the runtime config
        # instead. The rest of the call shape is confirmed.
        environmentVariables=container_env,
    )
    print("agentRuntimeArn:", resp.get("agentRuntimeArn"))
    print("Invoke with runtimeSessionId >= 33 chars and payload {\"prompt\": ...}.")


if __name__ == "__main__":
    main()
