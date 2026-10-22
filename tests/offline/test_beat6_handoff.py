"""Beat 6: three branches, not two.

1. check_financing_status fails and Temporal retries it mechanically until the
   fixed count exhausts. The model never sees the attempts.
2. The exhausted failure returns to the model as a tool result, and the model
   tries a different skill (check_inventory) as a different approach.
3. That does not resolve the need, so the model hands off. The Workflow parks on
   a Signal at zero Worker cost until the back office responds, then resumes.

The decision, approver, and time land in Event History as the signal and the
transcript line it writes, with no separate logging code.
"""

from __future__ import annotations

import asyncio

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from durable_agent.config import build_plugin
from durable_agent.session_client import signal_with_start
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from tests.offline._harness import (
    close_and_result,
    new_conversation_id,
    tool_calls,
    tool_results,
    wait_for_processed,
)


async def _wait_for_handoff(handle, timeout: float = 15.0) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        reason = await handle.query(SessionWorkflow.pending_handoff)
        if reason is not None:
            return reason
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("handoff never parked")
        await asyncio.sleep(0.05)


async def _max_activity_attempt(handle) -> int:
    history = await handle.fetch_history()
    attempts = [
        e.activity_task_started_event_attributes.attempt
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
    ]
    return max(attempts) if attempts else 0


async def test_three_branches_in_order(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        handle = await signal_with_start(client, conv, "Can I get financing for a car?", "handoff")

        # Branch three parks on the Signal; the back office then responds.
        reason = await _wait_for_handoff(handle)
        assert reason != ""
        await handle.signal(SessionWorkflow.respond_handoff, args=["approved", "back-office-1"])
        await wait_for_processed(handle, 1)

        # Mechanical retries reached the fixed count in Event History...
        max_attempt = await _max_activity_attempt(handle)

        result = await close_and_result(client, conv, handle)

    transcript = result["transcript"]

    # ...while the model saw the financing skill exactly once, as one failure.
    assert max_attempt >= 3
    assert tool_calls(transcript).count("check_financing_status") == 1

    # The three branches, in order.
    assert tool_calls(transcript) == [
        "check_financing_status",
        "check_inventory",
        "hand_off_to_back_office",
    ]
    results = dict(tool_results(transcript))
    assert results["check_financing_status"] == "failed"
    assert results["check_inventory"] == "ok"

    # Handoff parked and then resumed, with the decision and approver recorded.
    system_texts = [e["text"] for e in transcript if e["kind"] == "system"]
    assert any("handed off to back office" in t for t in system_texts)
    assert any("back office responded: approved by back-office-1" in t for t in system_texts)
