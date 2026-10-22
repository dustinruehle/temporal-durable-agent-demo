"""Request/response over the durable session, for the AgentCore invocation path.

AgentCore's invocation is a single request that expects a reply, but the session
is an async Workflow. This bridge delivers a message through the existing
Signal-With-Start path and then waits for the session's next reply by querying
the transcript. If the turn parks on a back-office handoff instead of replying,
it returns that state, so the caller can resolve the handoff and ask again. The
same Workflow Id is used across invocations, which is what lets a conversation
outlive an AgentCore session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from temporalio.client import Client

from durable_agent.session_client import signal_with_start
from durable_agent.workflows import SessionWorkflow


@dataclass
class InvocationResult:
    session_id: str
    status: str  # "replied" | "handed_off"
    reply: str = ""
    pending_handoff: str | None = None


async def _replies(handle) -> list[dict]:
    try:
        transcript = await handle.query(SessionWorkflow.transcript)
    except Exception:  # noqa: BLE001
        return []
    return [e for e in transcript if e["kind"] == "agent_reply"]


async def invoke_session(
    client: Client,
    session_id: str,
    text: str,
    scenario: str | None = None,
    fail_count: int | None = None,
    timeout: float = 120.0,
    poll: float = 0.1,
) -> InvocationResult:
    """Deliver a message and wait for the session's next reply.

    The session id is the Workflow Id, so an invocation after an AgentCore session
    has ended resumes the same conversation. Returns "handed_off" if the turn parks
    on a back-office handoff rather than producing a reply.
    """
    handle = client.get_workflow_handle(session_id)
    prior = len(await _replies(handle))

    await signal_with_start(
        client, session_id, text, scenario=scenario or "happy", fail_count=fail_count
    )

    deadline = asyncio.get_event_loop().time() + timeout
    handle = client.get_workflow_handle(session_id)
    while asyncio.get_event_loop().time() < deadline:
        replies = await _replies(handle)
        if len(replies) > prior:
            return InvocationResult(session_id, "replied", reply=replies[-1]["text"])
        # A parked handoff will not produce a reply until the back office responds.
        try:
            pending = await handle.query(SessionWorkflow.pending_handoff)
        except Exception:  # noqa: BLE001
            pending = None
        if pending is not None:
            return InvocationResult(session_id, "handed_off", pending_handoff=pending)
        await asyncio.sleep(poll)

    raise TimeoutError(f"no reply for session {session_id} within {timeout}s")
