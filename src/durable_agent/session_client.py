"""Client-side helpers for driving a session, reused by the gateway and tests.

The whole inbound path is Signal-With-Start with the conversation id as the
Workflow Id. There is no exists-check, no lookup table, and no lock: the Temporal
Service resolves the race. The gateway is a thin HTTP wrapper over this call, so a
curl and a dashboard button traverse the exact same code.
"""

from __future__ import annotations

from temporalio.client import Client, WorkflowHandle

from domain.config import TASK_QUEUE_AGENT
from durable_agent.config import build_session_input
from durable_agent.workflows import SessionWorkflow


async def signal_with_start(
    client: Client,
    conversation_id: str,
    text: str,
    scenario: str = "happy",
    fail_count: int | None = None,
) -> WorkflowHandle:
    """Start the session if absent, otherwise deliver the message to the running one.

    Setting start_signal makes this Signal-With-Start: the Temporal Service starts
    the Workflow if the conversation id is not running and always delivers the
    message signal, whether the execution is new or already running. No
    exists-check and no lock in the caller.
    """
    inp = build_session_input(scenario, fail_count=fail_count, conversation_id=conversation_id)
    return await client.start_workflow(
        SessionWorkflow.run,
        inp,
        id=conversation_id,
        task_queue=TASK_QUEUE_AGENT,
        start_signal="deliver_message",
        start_signal_args=[text],
    )
