"""Beat 8: Continue-As-New as the journey grows.

Same Workflow Id, client handle unchanged, and a pending follow-up deadline
survives the boundary. The follow-up carries as an absolute time, so a delay at
the boundary cannot shift it: the wake-up still fires at the original moment.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from durable_agent.config import build_plugin
from durable_agent.session_client import signal_with_start
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from tests.offline._harness import (
    new_conversation_id,
    start_session,
    wait_for_new_run,
    wait_for_processed,
    wait_for_transcript_kind,
)

_THREE_DAYS = 3 * 24 * 60 * 60


async def test_continue_as_new_preserves_id_and_carries_messages(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        handle = await signal_with_start(client, conv, "buying under 15k", "happy")
        await wait_for_processed(handle, 1)

        latest = client.get_workflow_handle(conv)
        run_a = (await latest.describe()).run_id
        await latest.signal(SessionWorkflow.force_continue_as_new)
        run_b = await wait_for_new_run(latest, run_a)

        # Same Workflow Id (the same conversation handle resolves both runs), a
        # new run, and the conversation carried across.
        assert run_b != run_a
        status = await latest.query(SessionWorkflow.status)
        assert status["carried_messages"] > 0

        # The new run still handles messages on the same handle.
        await latest.signal(SessionWorkflow.deliver_message, "any SUVs?")
        await wait_for_processed(latest, 1)
        await latest.signal(SessionWorkflow.close_session)


async def test_follow_up_survives_continue_as_new(time_skip_env: WorkflowEnvironment):
    client = time_skip_env.client
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        with time_skip_env.auto_time_skipping_disabled():
            handle = await start_session(
                client, "deferred_follow_up", conv, follow_up_seconds=_THREE_DAYS
            )
            await handle.signal(SessionWorkflow.deliver_message, "call me later")
            await wait_for_processed(handle, 1)

            latest = client.get_workflow_handle(conv)
            before = await latest.query(SessionWorkflow.status)
            deadline_before = before["follow_up_deadline_epoch"]
            assert deadline_before is not None

            # Force the roll-over while the follow-up is still pending.
            run_a = (await latest.describe()).run_id
            await latest.signal(SessionWorkflow.force_continue_as_new)
            await wait_for_new_run(latest, run_a)

            # Re-armed in the new run at the same absolute deadline.
            after = await latest.query(SessionWorkflow.status)
            assert after["follow_up_armed"] is True
            assert after["follow_up_deadline_epoch"] == deadline_before

            # Advancing past the original deadline still fires the wake-up.
            await time_skip_env.sleep(timedelta(seconds=_THREE_DAYS + 60))
            transcript = await wait_for_transcript_kind(latest, "outbound")

    assert any(e["kind"] == "outbound" for e in transcript)
