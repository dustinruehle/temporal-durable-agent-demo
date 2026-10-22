"""The AgentCore Runtime as a Temporal Serverless Worker, on the Bedrock AgentCore SDK.

This runtime holds no persistent Worker. Each invocation of the AgentCore endpoint
spins up an ephemeral Temporal Worker, does its work, and drains, so AgentCore's idle
timer can reap the microVM. That is the Serverless Workers model: when a Task is on
the queue and no Worker is polling, Temporal's Worker Controller Instance invokes the
runtime endpoint (via a configured compute provider), the Worker starts inside
AgentCore, drains the queue, and shuts itself down. No local Worker is required.

The entrypoint is dual-mode so nothing is lost:

- Empty payload (the WCI / Serverless Workers path): start a Worker, poll until the
  queue is idle for the debounce window, then drain and return.
- Payload with a message (the request/response coda and direct testing): start the
  same ephemeral Worker, deliver the message through Signal-With-Start, wait for the
  turn's reply, then drain and return the reply.

The demo's session model is unchanged: the runtime session id is the Workflow Id and
each message is one Signal-With-Start, so the session survives across invocations and
resumes with no rehydration.
"""

from __future__ import annotations

import os
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.models import PingStatus
from starlette.responses import JSONResponse
from temporalio.client import Client

from durable_agent.worker import running_workers
from runtime.idle import ActivityTracker
from runtime.session_bridge import invoke_session

# The health strings AgentCore expects, taken from the SDK so they cannot drift.
HEALTHY = PingStatus.HEALTHY.value
HEALTHY_BUSY = PingStatus.HEALTHY_BUSY.value

# The session id header AgentCore sends; the SDK reads it into context.session_id.
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
MIN_SESSION_ID_LEN = 33

# How long the ephemeral Worker keeps polling after it goes idle, on the empty-payload
# (Serverless Workers) path. The message path does not wait: it drains as soon as the
# turn's reply is in hand.
DEFAULT_DEBOUNCE = 60.0


MIN_DEBOUNCE = 1.0
MAX_DEBOUNCE = 3600.0


def _debounce(payload: Any = None) -> float:
    """Idle seconds the ephemeral Worker keeps polling before it drains.

    Order of precedence, so the drain window is tunable live during a demo without a
    redeploy: an explicit `debounce` in the invoke payload wins, then
    AGENTCORE_DEBOUNCE_SECONDS, then the default. A short value (for example 30) makes
    the drain happen fast enough to watch on stage; a long value holds the Worker warm.
    """
    raw: Any = None
    if isinstance(payload, dict) and payload.get("debounce") is not None:
        raw = payload.get("debounce")
    if raw is None:
        raw = os.environ.get("AGENTCORE_DEBOUNCE_SECONDS", DEFAULT_DEBOUNCE)
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DEBOUNCE
    return max(MIN_DEBOUNCE, min(secs, MAX_DEBOUNCE))


def _message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    # Accept the AWS-canonical "prompt" plus a "text" alias.
    return str(payload.get("prompt") or payload.get("text") or "").strip()


def make_app(client: Client, plugin, *, deployment_config=None) -> BedrockAgentCoreApp:
    """Build the AgentCore serverless-worker app bound to a Temporal client and plugin.

    deployment_config carries Worker Versioning when the compute provider is wired
    (Serverless Workers require it); it is None for local and offline runs.
    """
    app = BedrockAgentCoreApp()

    @app.entrypoint
    async def invoke(payload: Any, context: Any) -> Any:
        message = _message(payload)

        # Validate the request/response path before spinning up a Worker, so a bad
        # request costs nothing.
        session_id = context.session_id
        if message and (not session_id or len(session_id) < MIN_SESSION_ID_LEN):
            return JSONResponse(
                {
                    "error": f"{SESSION_HEADER} header is required and must be at "
                    f"least {MIN_SESSION_ID_LEN} characters"
                },
                status_code=400,
            )

        tracker = ActivityTracker()

        # Provide compute for this invocation only: an ephemeral Worker that drains
        # when the work is done. No persistent Worker lives in this container.
        async with running_workers(
            client, plugin, interceptors=[tracker], deployment_config=deployment_config
        ):
            if not message:
                # Serverless Workers / WCI path: poll until the queue is quiet, drain.
                debounce = _debounce(payload)
                await tracker.wait_until_idle(debounce)
                return {"status": "drained", "message": "worker idle; drained", "debounce": debounce}

            # Request/response path: deliver one turn and return its reply.
            task_id = app.add_async_task("session-turn")
            try:
                result = await invoke_session(
                    client,
                    session_id,
                    message,
                    scenario=payload.get("scenario"),
                    fail_count=payload.get("fail_count"),
                )
            finally:
                app.complete_async_task(task_id)

            return {
                "session_id": result.session_id,
                "status": result.status,
                "response": result.reply,
                "pending_handoff": result.pending_handoff,
            }

    return app
