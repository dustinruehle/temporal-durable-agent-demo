"""Beat 7: MODEL_QUEUE_RPS lowered, and model calls pace.

The cap is the task-queue-wide rate on the model-calls queue, enforced by the
Temporal Service rather than at the call site, replacing a hand-rolled
tokens-per-minute limiter. This offline test verifies the enforcement itself: with
the cap set, the aggregate model-call rate stays under it and the work is paced.

The cross-replica aggregate, two model-calls Worker processes together staying
under the cap with no coordination, is a live check, because two Workers on one
task queue require separate processes (the bridge forbids two on one runtime).
"""

from __future__ import annotations

import asyncio

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle

from durable_agent.config import build_plugin
from durable_agent.session_client import signal_with_start
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from tests.offline._harness import new_conversation_id, wait_for_processed

_RPS = 6.0
_SESSIONS = 8


async def _count_model_calls(handle: WorkflowHandle) -> int:
    history = await handle.fetch_history()
    return sum(
        1
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name == "invoke_model"
    )


async def test_model_rate_cap_paces_model_calls(client: Client):
    plugin = build_plugin("mock")
    convs = [new_conversation_id() for _ in range(_SESSIONS)]

    async with running_workers(client, plugin, model_replicas=1, model_queue_rps=_RPS):
        loop = asyncio.get_event_loop()
        start = loop.time()
        handles = await asyncio.gather(
            *[signal_with_start(client, c, "buying under 15k", "happy") for c in convs]
        )
        await asyncio.gather(*[wait_for_processed(h, 1, timeout=30) for h in handles])
        elapsed = loop.time() - start

        total_model_calls = sum([await _count_model_calls(h) for h in handles])

        for c in convs:
            await client.get_workflow_handle(c).signal(SessionWorkflow.close_session)

    observed_rate = total_model_calls / elapsed
    # The aggregate rate across both replicas stays under the cap (with headroom
    # for the limiter's initial burst).
    assert observed_rate <= _RPS * 1.5, f"observed {observed_rate:.1f} rps over cap {_RPS}"
    # And the cap actually paced the work, rather than everything finishing at once.
    assert elapsed >= (total_model_calls / _RPS) * 0.5
