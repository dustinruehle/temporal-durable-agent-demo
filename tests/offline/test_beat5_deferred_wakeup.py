"""Beat 5: deferred wake-up, the money moment.

The agent arms a durable follow-up. The Workflow holds the deadline and awaits it
with an updatable timer, so a Signal can shorten, fast-forward, or cancel it.
When it fires, the follow-up Activity runs and the agent posts an outbound
customer message into the transcript. These tests run under the time-skipping
environment with automatic skipping disabled, so the deadline moves only when the
test advances it or a Signal moves it. None of them fake time inside the Workflow.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.testing import WorkflowEnvironment

from durable_agent.config import build_plugin
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from tests.offline._harness import (
    new_conversation_id,
    start_session,
    wait_for_processed,
    wait_for_transcript_kind,
)

_THREE_DAYS = 3 * 24 * 60 * 60


async def _arm(client, conv):
    handle = await start_session(
        client, "deferred_follow_up", conv, follow_up_seconds=_THREE_DAYS
    )
    await handle.signal(SessionWorkflow.deliver_message, "I'll think about it, call me later")
    await wait_for_processed(handle, 1)
    status = await handle.query(SessionWorkflow.status)
    assert status["follow_up_armed"] is True
    return handle


async def test_follow_up_fires_and_posts_outbound(time_skip_env: WorkflowEnvironment):
    client = time_skip_env.client
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        with time_skip_env.auto_time_skipping_disabled():
            handle = await _arm(client, conv)
            # Advance past the three-day deadline; the timer fires.
            await time_skip_env.sleep(timedelta(seconds=_THREE_DAYS + 60))
            transcript = await wait_for_transcript_kind(handle, "outbound")

    outbound = [e for e in transcript if e["kind"] == "outbound"]
    assert len(outbound) == 1
    assert outbound[0]["text"].strip() != ""


async def test_fast_forward_shortens_the_wake_up(time_skip_env: WorkflowEnvironment):
    client = time_skip_env.client
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        with time_skip_env.auto_time_skipping_disabled():
            handle = await _arm(client, conv)
            # Move the deadline to now with a real Signal, without waiting three days.
            await handle.signal(SessionWorkflow.fast_forward_follow_up)
            transcript = await wait_for_transcript_kind(handle, "outbound")

    assert any(e["kind"] == "outbound" for e in transcript)


async def test_customer_reply_cancels_the_follow_up(time_skip_env: WorkflowEnvironment):
    client = time_skip_env.client
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        with time_skip_env.auto_time_skipping_disabled():
            handle = await _arm(client, conv)
            # The customer re-engages before the wake-up; the follow-up cancels.
            await handle.signal(SessionWorkflow.customer_replies_now, "actually I'm ready now")
            await wait_for_processed(handle, 2)
            status = await handle.query(SessionWorkflow.status)
            assert status["follow_up_armed"] is False

            # Advancing past the original deadline produces no outbound.
            await time_skip_env.sleep(timedelta(seconds=_THREE_DAYS + 60))
            transcript = await handle.query(SessionWorkflow.transcript)

    assert not any(e["kind"] == "outbound" for e in transcript)
    assert any(e["kind"] == "system" and "cancelled" in e["text"] for e in transcript)
