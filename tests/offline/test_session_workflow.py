"""Stage 2 happy path: one message, one skill, a reply, against the mock model."""

from __future__ import annotations

from temporalio.client import Client

from tests.offline._harness import kinds, run_one_message, tool_calls, tool_results


async def test_happy_path_one_message_one_skill_one_reply(client: Client):
    result = await run_one_message(client, "happy", "I'm looking to buy a car under 15k")

    assert result["processed"] == 1
    transcript = result["transcript"]

    # The customer message, one inventory lookup with an ok result, and a reply.
    assert tool_calls(transcript) == ["check_inventory"]
    assert tool_results(transcript) == [("check_inventory", "ok")]
    assert kinds(transcript)[0] == "customer_message"
    assert kinds(transcript)[-1] == "agent_reply"

    reply = [e for e in transcript if e["kind"] == "agent_reply"][-1]
    assert reply["text"].strip() != ""
