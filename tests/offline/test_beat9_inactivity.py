"""Beat 9: inactivity and rehydration.

A naive agent keeps the session in the process, so a 15-minute idle reap (the
AgentCore microVM shutting down after a quiet period) wipes it, and the next turn
must rehydrate from a store or start blind. The durable session lives in Temporal,
so the same idle gap costs nothing: a follow-up resumes the same execution with the
earlier turn intact, no rehydration code required.

The idle reap is mimicked instantly here by discarding the in-memory harness (a
fresh process) rather than waiting the real fifteen minutes.
"""

from __future__ import annotations

from temporalio.client import Client

from durable_agent.config import build_plugin
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from harness_plain.loop import Harness as PlainHarness

from ._harness import new_conversation_id, start_session, wait_for_processed


def _customer_messages(transcript: list[dict]) -> list[str]:
    return [e["text"] for e in transcript if e["kind"] == "customer_message"]


async def test_naive_session_is_lost_on_idle_reap():
    # A turn is handled and recorded, but only in the process's memory.
    harness = PlainHarness()
    await harness.handle_message("idle", "I am looking to buy under 15k", "happy", 0)
    assert any("15k" in t for t in _customer_messages(harness.transcript()))

    # The 15-minute idle reap: the microVM (process) is gone, so a fresh process
    # starts with no memory of the session.
    reaped = PlainHarness()
    assert _customer_messages(reaped.transcript()) == []

    # A follow-up lands on the fresh process. Without rehydration-from-store code the
    # earlier turn is lost; only the follow-up is present.
    await reaped.handle_message("idle", "book the first one", "happy", 0)
    assert not any("15k" in t for t in _customer_messages(reaped.transcript()))


async def test_durable_session_survives_idle_and_resumes(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id("idle")
    async with running_workers(client, plugin):
        handle = await start_session(client, "happy", conv)
        await handle.signal(SessionWorkflow.deliver_message, "I am looking to buy under 15k")
        await wait_for_processed(handle, 1)

        # While idle the session parks; the Service holds it, so no process is needed
        # to keep it alive. This is exactly what a reaped microVM does not take down.
        desc = await handle.describe()
        assert desc.status is not None and desc.status.name == "RUNNING"
        first_run = desc.run_id

        # A follow-up after the idle gap resumes the SAME execution with the earlier
        # turn still present: the conversation was never reloaded from a store.
        await handle.signal(SessionWorkflow.deliver_message, "book the first one")
        await wait_for_processed(handle, 2)
        transcript = await handle.query(SessionWorkflow.transcript)
        assert any("15k" in t for t in _customer_messages(transcript))
        assert (await handle.describe()).run_id == first_run

        await client.get_workflow_handle(conv).signal(SessionWorkflow.close_session)
