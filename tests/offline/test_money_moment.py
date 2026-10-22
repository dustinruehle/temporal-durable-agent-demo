"""The money moment, verified programmatically.

With a session parked on the handoff Signal, stopping the Workers leaves the
Workflow RUNNING on the server (the PLATFORM lane's describe survives). Restarting
the Workers resumes execution from the last recorded step, and no completed model
call is repeated: every model Activity in history is scheduled, started, and
completed exactly once.
"""

from __future__ import annotations

import asyncio

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowExecutionStatus

from durable_agent.config import build_plugin
from durable_agent.session_client import signal_with_start
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from tests.offline._harness import close_and_result, new_conversation_id, wait_for_processed


async def _wait_parked(handle, timeout: float = 30.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while (await handle.query(SessionWorkflow.pending_handoff)) is None:
        assert asyncio.get_event_loop().time() < deadline, "handoff never parked"
        await asyncio.sleep(0.05)


def _model_activity_counts(history) -> tuple[int, int, int]:
    """Return (scheduled, started, completed) counts for the model Activity.

    Started and completed events do not carry the Activity type, so correlate them
    to their scheduled event by scheduled_event_id.
    """
    model_scheduled_ids: set[int] = set()
    for e in history.events:
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            if e.activity_task_scheduled_event_attributes.activity_type.name == "invoke_model":
                model_scheduled_ids.add(e.event_id)
    started = sum(
        1
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
        and e.activity_task_started_event_attributes.scheduled_event_id in model_scheduled_ids
    )
    completed = sum(
        1
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED
        and e.activity_task_completed_event_attributes.scheduled_event_id in model_scheduled_ids
    )
    return len(model_scheduled_ids), started, completed


async def test_worker_death_survives_and_resumes_without_repeating_model_calls(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()

    # Park the session on the handoff Signal, with model calls already done.
    async with running_workers(client, plugin):
        handle = await signal_with_start(client, conv, "financing please", "handoff")
        await _wait_parked(handle)

    # Workers are down. The PLATFORM lane's describe still reports RUNNING.
    desc = await handle.describe()
    assert desc.status == WorkflowExecutionStatus.RUNNING

    # Restart the Workers; the back office responds and execution resumes.
    async with running_workers(client, plugin):
        await handle.signal(SessionWorkflow.respond_handoff, args=["approved", "back office"])
        await wait_for_processed(handle, 1)
        result = await close_and_result(client, conv, handle)

    assert result["processed"] == 1

    # No completed model call was repeated: each model Activity was scheduled,
    # started, and completed exactly once.
    history = await handle.fetch_history()
    scheduled, started, completed = _model_activity_counts(history)
    assert scheduled > 0
    assert scheduled == started == completed
