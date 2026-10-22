"""Replay safety: replaying a Workflow's history must not diverge.

A Replayer runs the Workflow code against a recorded history and fails on any
non-determinism. Covering the happy path and the handoff path exercises the model
loop, the skill Activities, the interrupt-and-resume handoff, and the transcript
hook under replay.
"""

from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Replayer

from durable_agent.config import build_plugin
from durable_agent.session_client import signal_with_start
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from tests.offline._harness import close_and_result, new_conversation_id, wait_for_processed


async def _replay(plugin, history) -> None:
    await Replayer(workflows=[SessionWorkflow], plugins=[plugin]).replay_workflow(history)


async def test_replay_happy_path(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        handle = await signal_with_start(client, conv, "buying under 15k", "happy")
        await wait_for_processed(handle, 1)
        await close_and_result(client, conv, handle)
        history = await handle.fetch_history()
    await _replay(plugin, history)


async def test_replay_handoff_path(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        handle = await signal_with_start(client, conv, "financing please", "handoff")
        # Wait for the handoff to park, then resolve it.
        deadline = asyncio.get_event_loop().time() + 20
        while (await handle.query(SessionWorkflow.pending_handoff)) is None:
            assert asyncio.get_event_loop().time() < deadline, "handoff never parked"
            await asyncio.sleep(0.05)
        await handle.signal(SessionWorkflow.respond_handoff, args=["approved", "back-office-1"])
        await wait_for_processed(handle, 1)
        await close_and_result(client, conv, handle)
        history = await handle.fetch_history()
    await _replay(plugin, history)
