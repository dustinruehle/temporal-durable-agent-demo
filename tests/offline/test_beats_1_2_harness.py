"""Beats 1 and 2: the stage 0 and stage 1 harnesses.

Stage 0 handles a message entirely in-process, so its transcript lives only in
memory. Stage 1 runs exactly one skill, check_financing_status, as a Standalone
Activity from the client, so that one step is durable even though the harness is
not a Worker and the conversation is still held in memory.

The kill-mid-call behavior (beat 1 loses everything, beat 2 keeps the Activity) is
driven end to end by scripts/beat.py, which owns the harness process lifecycle.
"""

from __future__ import annotations

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from domain import skills
from domain.config import TASK_QUEUE_AGENT
from harness_plain.loop import Harness as PlainHarness
from harness_wrapped.loop import Harness as WrappedHarness


def _tool_calls(transcript: list[dict]) -> list[str]:
    return [e["tool"] for e in transcript if e["kind"] == "tool_call"]


async def test_stage0_handles_message_in_memory():
    harness = PlainHarness()
    reply = await harness.handle_message("s1", "what is my financing status?", "handoff", 0)

    transcript = harness.transcript()
    assert "check_financing_status" in _tool_calls(transcript)
    assert reply.strip() != ""
    assert transcript[0]["kind"] == "customer_message"
    assert transcript[-1]["kind"] == "agent_reply"


async def test_stage1_runs_skill_as_standalone_activity(
    client: Client, monkeypatch: pytest.MonkeyPatch
):
    # Point the harness client at the test dev server, which has the Standalone
    # Activities flag enabled, and run a Worker that can execute the skill.
    target = client.service_client.config.target_host
    monkeypatch.setenv("TEMPORAL_ADDRESS", target)

    async with Worker(client, task_queue=TASK_QUEUE_AGENT, activities=skills.ALL_SKILLS):
        harness = WrappedHarness()
        reply = await harness.handle_message("s1", "what is my financing status?", "handoff", 0)

    transcript = harness.transcript()
    # The skill ran as a Standalone Activity and returned a result (terminal
    # state), and the harness produced a reply.
    assert "check_financing_status" in _tool_calls(transcript)
    financing_results = [
        e for e in transcript if e["kind"] == "tool_result" and e["tool"] == "check_financing_status"
    ]
    assert financing_results and financing_results[-1]["status"] == "ok"
    assert reply.strip() != ""
