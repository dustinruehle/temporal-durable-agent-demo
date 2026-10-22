"""The mock model drives real tool calls deterministically across scenarios.

These run a plain strands.Agent (no Temporal) so they exercise the decider and
the streaming protocol directly, the same way stage 0 uses the mock. The Workflow
level is covered separately.
"""

from __future__ import annotations

from strands import Agent, tool

from domain import backend, prompt
from domain.mock_model import MockModel
from domain.prompt import FOLLOW_UP_DUE_MARKER
from domain.scenarios import load_scenario


@tool
def check_inventory(max_price: int = 15000) -> dict:
    """Find vehicles under a budget."""
    return {"vehicles": backend.find_inventory(max_price=max_price)}


@tool
def check_financing_status(application_id: str, fail_attempts: int = 0) -> dict:
    """Look up a financing application; raise to simulate exhausted retries."""
    if fail_attempts:
        raise RuntimeError("financing backend unavailable")
    return backend.financing_status(application_id)


@tool
def schedule_follow_up(note: str = "") -> str:
    """Arm a follow-up (stub)."""
    return "follow-up armed"


@tool
def hand_off_to_back_office(reason: str = "") -> str:
    """Hand off to the back office (stub)."""
    return "handed off and resolved"


@tool
def send_customer_message(text: str = "", conversation_id: str = "") -> str:
    """Send an outbound message (stub)."""
    return "sent"


def _calls(agent: Agent) -> list[str]:
    names = []
    for m in agent.messages:
        for c in m.get("content", []):
            if isinstance(c, dict) and "toolUse" in c:
                names.append(c["toolUse"]["name"])
    return names


def _agent(scenario_name: str, tools: list, fail: int | None = None) -> Agent:
    scenario = load_scenario(scenario_name)
    sys_prompt = prompt.build_system_prompt(scenario, financing_fail_override=fail)
    return Agent(model=MockModel(max_price=scenario.max_price), tools=tools, system_prompt=sys_prompt)


async def test_happy_calls_inventory_then_replies():
    agent = _agent("happy", [check_inventory])
    result = await agent.invoke_async("I'm looking to buy a car under 15k")
    assert _calls(agent) == ["check_inventory"]
    assert result.stop_reason == "end_turn"
    assert str(result).strip() != ""


async def test_handoff_runs_all_three_branches_in_order():
    agent = _agent(
        "handoff",
        [check_financing_status, check_inventory, hand_off_to_back_office],
        fail=3,
    )
    result = await agent.invoke_async("Can I get financing for a car?")
    # Branch one (financing fails), branch two (different skill), branch three (handoff).
    assert _calls(agent) == [
        "check_financing_status",
        "check_inventory",
        "hand_off_to_back_office",
    ]
    assert result.stop_reason == "end_turn"


async def test_deferred_arms_follow_up_then_sends_outbound_when_due():
    agent = _agent("deferred_follow_up", [check_inventory, schedule_follow_up, send_customer_message])
    await agent.invoke_async("I want to think about it, call me later")
    assert _calls(agent) == ["check_inventory", "schedule_follow_up"]

    # The follow-up fires: the Workflow re-invokes with the due marker.
    await agent.invoke_async(f"{FOLLOW_UP_DUE_MARKER} the scheduled follow-up is due")
    assert _calls(agent)[-1] == "send_customer_message"


async def test_decider_is_deterministic_across_repeated_runs():
    first = None
    for _ in range(3):
        agent = _agent("handoff", [check_financing_status, check_inventory, hand_off_to_back_office], fail=3)
        await agent.invoke_async("Can I get financing?")
        calls = _calls(agent)
        if first is None:
            first = calls
        assert calls == first
