"""Env-driven configuration and switches for stage 2.

Everything the Workflow needs is resolved here, on the client side, and passed
into the Workflow as a fully-resolved SessionInput. The Workflow itself reads no
environment and no files, so replay stays deterministic. Local to Cloud is a pure
env change because the client connection goes through
ClientConfig.load_client_connect_config().
"""

from __future__ import annotations

import os
from collections.abc import Callable

from strands.models import Model
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin
from temporalio.envconfig import ClientConfig

from domain import config as domain_config
from domain import prompt as prompt_mod
from domain.mock_model import MockModel
from domain.scenarios import load_scenario
from durable_agent.session_types import SessionInput

__all__ = ["SessionInput"]

# --- switches ------------------------------------------------------------------


def temporal_target() -> str:
    return os.environ.get("TEMPORAL_TARGET", "local").strip().lower()


def model_mode() -> str:
    return os.environ.get("MODEL_MODE", "mock").strip().lower()


def skills_mode() -> str:
    return os.environ.get("SKILLS_MODE", "mock").strip().lower()


def runtime_host() -> str:
    return os.environ.get("RUNTIME_HOST", "local").strip().lower()


# --- the Workflow input, fully resolved on the client side -------------------


def build_session_input(
    scenario_name: str,
    fail_count: int | None = None,
    conversation_id: str = "",
) -> SessionInput:
    """Resolve a scenario and the env switches into a Workflow input."""
    scenario = load_scenario(scenario_name)
    resolved_fail = scenario.financing_fail_count() if fail_count is None else fail_count
    system_prompt = prompt_mod.build_system_prompt(scenario, financing_fail_override=fail_count)
    return SessionInput(
        system_prompt=system_prompt,
        model=model_mode(),
        scenario=scenario_name,
        conversation_id=conversation_id,
        fail_count=resolved_fail,
        follow_up_seconds=domain_config.follow_up_seconds(),
        track_follow_up=domain_config.follow_up_search_attribute(),
    )


# --- model registration ------------------------------------------------------


def _mock_factory() -> Model:
    return MockModel()


def _anthropic_factory() -> Model:
    # Disable the provider client's own retries so Temporal owns model retries.
    from strands.models.anthropic import AnthropicModel

    return AnthropicModel(
        client_args={"api_key": os.environ["ANTHROPIC_API_KEY"], "max_retries": 0},
        model_id=os.environ.get("ANTHROPIC_MODEL_ID", "claude-sonnet-4-5"),
        max_tokens=1024,
    )


def _bedrock_factory() -> Model:
    # Mirror the plugin default: disable Botocore retries so Temporal owns them.
    from botocore.config import Config as BotocoreConfig
    from strands.models.bedrock import BedrockModel

    kwargs: dict = {
        "boto_client_config": BotocoreConfig(retries={"max_attempts": 0}),
        "region_name": os.environ.get("AWS_REGION", "us-east-1"),
    }
    # Optional override so you can point at whichever Bedrock model your account is
    # granted; without it, Strands uses its own default inference profile.
    model_id = os.environ.get("BEDROCK_MODEL_ID")
    if model_id:
        kwargs["model_id"] = model_id
    return BedrockModel(**kwargs)


def model_mapping(mode: str | None = None) -> dict[str, Callable[[], Model]]:
    mode = mode or model_mode()
    if mode == "anthropic":
        return {"anthropic": _anthropic_factory}
    if mode == "bedrock":
        return {"bedrock": _bedrock_factory}
    return {"mock": _mock_factory}


def build_plugin(mode: str | None = None) -> StrandsPlugin:
    return StrandsPlugin(models=model_mapping(mode))


def worker_deployment_config():
    """Return a WorkerDeploymentConfig when Worker Versioning is configured, else None.

    Serverless Workers require Worker Versioning: Temporal only invokes a runtime for
    a Worker Deployment Version that has a compute provider. We gate it on env so the
    local demo and the offline suite run unversioned (None), and the AgentCore
    serverless runtime turns it on by setting TEMPORAL_DEPLOYMENT_NAME and
    TEMPORAL_BUILD_ID once the compute provider is wired.
    """
    name = os.environ.get("TEMPORAL_DEPLOYMENT_NAME")
    build_id = os.environ.get("TEMPORAL_BUILD_ID")
    if not name or not build_id:
        return None
    from temporalio.common import VersioningBehavior
    from temporalio.worker import WorkerDeploymentConfig, WorkerDeploymentVersion

    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(deployment_name=name, build_id=build_id),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )


def validate_model_credentials(mode: str | None = None) -> None:
    """Fail cleanly at startup when a non-mock provider is missing its creds.

    A provider with missing credentials fails startup with a message naming the
    missing variable, not with a stack trace deep in an Activity.
    """
    mode = mode or model_mode()
    if mode == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "MODEL_MODE=anthropic requires ANTHROPIC_API_KEY. "
            "Set it, or run with MODEL_MODE=mock."
        )
    if mode == "bedrock":
        # Resolve credentials through the real boto3 chain, which covers explicit
        # keys, a named profile, and a role delivered by the container or instance
        # metadata endpoint (ECS, AgentCore, EKS/IRSA). This avoids guessing which
        # env vars a given hosting environment sets.
        import boto3

        if boto3.Session().get_credentials() is None:
            raise SystemExit(
                "MODEL_MODE=bedrock found no AWS credentials via the standard chain "
                "(explicit keys, AWS_PROFILE, or an attached role). "
                "Set them, or run with MODEL_MODE=mock."
            )


def guard_runtime_host() -> None:
    """agentcore hosting runs through the AgentCore runtime app, not run.sh.

    The AgentCore Runtime hosts the shim (scripts/runtime.py), which starts the
    Worker in-process, so the standalone worker/gateway/dashboard path does not
    apply. Point run.sh callers at the shim rather than starting duplicate Workers.
    """
    if runtime_host() == "agentcore":
        raise SystemExit(
            "RUNTIME_HOST=agentcore runs through the AgentCore shim: "
            "python scripts/runtime.py, not run.sh."
        )


# --- client connection -------------------------------------------------------


async def connect_client() -> Client:
    """Connect using the env config idiom, defaulting to the local dev server.

    This is what makes local to Cloud a pure env change. Do not hand-roll a
    connect function. Cloud reads TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, and
    either TEMPORAL_API_KEY or the mTLS cert path pair through this call.
    """
    connect_config = ClientConfig.load_client_connect_config()
    connect_config.setdefault("target_host", "localhost:7233")
    # Opt-in worker/client identity so the dashboard can show where the Worker runs
    # (for example "agentcore-runtime"), which is how the serverless drain is made
    # visible. Unset by default, so local and offline runs keep the SDK default.
    identity = os.environ.get("TEMPORAL_IDENTITY")
    if identity:
        connect_config.setdefault("identity", identity)
    return await Client.connect(**connect_config)
