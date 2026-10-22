"""The mock model: a deterministic, replay-safe Strands Model that decides.

This is a real strands.models.Model implementation, registered in the plugin
under the name "mock", so mock mode goes through the same plugin path as a real
provider. It is not a canned string: it emits real tool calls so beats 5 and 6
fire, and it drives all three of beat 6's branches. Because it is a plain Strands
Model, the same class drops into stages 0 and 1, which have no plugin.

The model reads which scenario is running from a directive the system prompt
carries, then chooses the next move by inspecting the conversation so far. Every
decision is a pure function of the messages, so replay is deterministic. The
model sleeps the configured latency so a presenter can Ctrl-C a model call, but
its output never depends on time.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from strands.models import Model

from domain import config
from domain.markers import FOLLOW_UP_DUE_MARKER

# Tool names the model may emit. Skill tools plus the two in-workflow tools that
# only exist in stage 2. The model only calls a tool that is actually available.
TOOL_CHECK_INVENTORY = "check_inventory"
TOOL_SCHEDULE_INSPECTION = "schedule_inspection"
TOOL_CHECK_FINANCING = "check_financing_status"
TOOL_SEND_MESSAGE = "send_customer_message"
TOOL_SCHEDULE_FOLLOW_UP = "schedule_follow_up"
TOOL_HAND_OFF = "hand_off_to_back_office"

_DIRECTIVE_RE = re.compile(
    r"<!--sim scenario=(?P<scenario>\w+) finfail=(?P<finfail>\d+)"
    r"(?: maxprice=(?P<maxprice>\d+))?-->"
)

# A synthetic financing application id, used so the same id maps to the same
# deterministic backend decision.
_APP_ID = "APP-7742"


# --- streaming event emitters (Bedrock-shaped, per strands.types.streaming) ---


def _text_events(text: str) -> list[dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        _metadata(),
    ]


def _tool_events(tool_id: str, name: str, input_obj: dict) -> list[dict]:
    import json

    return [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": tool_id, "name": name}},
                "contentBlockIndex": 0,
            }
        },
        {
            "contentBlockDelta": {
                "delta": {"toolUse": {"input": json.dumps(input_obj)}},
                "contentBlockIndex": 0,
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        _metadata(),
    ]


def _metadata() -> dict:
    return {
        "metadata": {
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            "metrics": {"latencyMs": 0},
        }
    }


# --- conversation inspection --------------------------------------------------


def _parse_directive(system_prompt: str | None) -> tuple[str, int, int | None]:
    if system_prompt:
        m = _DIRECTIVE_RE.search(system_prompt)
        if m:
            maxprice = int(m.group("maxprice")) if m.group("maxprice") else None
            return m.group("scenario"), int(m.group("finfail")), maxprice
    return "happy", 0, None


def _summarize(messages: list[dict]) -> tuple[list[str], dict[str, str], str]:
    """Return (tool names called in order, last status by tool, last user text)."""
    id_to_name: dict[str, str] = {}
    calls: list[str] = []
    status_by_tool: dict[str, str] = {}
    last_user_text = ""
    for m in messages:
        role = m.get("role")
        for c in m.get("content", []):
            if not isinstance(c, dict):
                continue
            if "toolUse" in c:
                tu = c["toolUse"]
                id_to_name[tu.get("toolUseId", "")] = tu.get("name", "")
                calls.append(tu.get("name", ""))
            elif "toolResult" in c:
                tr = c["toolResult"]
                name = id_to_name.get(tr.get("toolUseId", ""), "")
                if name:
                    status_by_tool[name] = tr.get("status", "success")
            elif "text" in c and role == "user":
                last_user_text = c["text"]
    return calls, status_by_tool, last_user_text


# --- the decider --------------------------------------------------------------


def _decide(
    scenario: str,
    finfail: int,
    available: set[str],
    calls: list[str],
    status_by_tool: dict[str, str],
    last_user_text: str,
    max_price: int,
) -> list[dict]:
    called = set(calls)
    follow_up_due = FOLLOW_UP_DUE_MARKER in last_user_text
    tid = f"tool-{len(calls)}"

    if scenario == "deferred_follow_up":
        if follow_up_due:
            if TOOL_SEND_MESSAGE in available and TOOL_SEND_MESSAGE not in called:
                return _tool_events(
                    tid,
                    TOOL_SEND_MESSAGE,
                    {"text": "Checking in on the vehicles you looked at. Still interested?"},
                )
            return _text_events("I have reached out to the customer with a follow-up.")
        if TOOL_CHECK_INVENTORY in available and TOOL_CHECK_INVENTORY not in called:
            return _tool_events(tid, TOOL_CHECK_INVENTORY, {"max_price": max_price})
        if TOOL_SCHEDULE_FOLLOW_UP in available and TOOL_SCHEDULE_FOLLOW_UP not in called:
            return _tool_events(
                tid,
                TOOL_SCHEDULE_FOLLOW_UP,
                {"note": "Customer wants time to decide"},
            )
        return _text_events("No rush. I will check back with you later.")

    if scenario == "handoff":
        if TOOL_CHECK_FINANCING in available and TOOL_CHECK_FINANCING not in called:
            return _tool_events(
                tid,
                TOOL_CHECK_FINANCING,
                {"application_id": _APP_ID, "fail_attempts": finfail},
            )
        fin_failed = status_by_tool.get(TOOL_CHECK_FINANCING) == "error"
        if not fin_failed:
            # Financing resolved (stages 0 and 1, or finfail=0): just answer.
            return _text_events("Your financing application is on file. Here is the status.")
        # Branch two: a different approach, a lower-priced vehicle.
        if TOOL_CHECK_INVENTORY in available and TOOL_CHECK_INVENTORY not in called:
            return _tool_events(tid, TOOL_CHECK_INVENTORY, {"max_price": max_price})
        # Branch three: the alternate did not resolve the need, so hand off.
        if TOOL_HAND_OFF in available and TOOL_HAND_OFF not in called:
            return _tool_events(
                tid,
                TOOL_HAND_OFF,
                {
                    "reason": "Financing unavailable and no suitable lower-priced "
                    "vehicle. Needs human review."
                },
            )
        if TOOL_HAND_OFF in called:
            return _text_events(
                "Our back office has reviewed your case and will follow up with next steps."
            )
        # No handoff tool available (stages 0 and 1): close politely.
        return _text_events("I could not complete that automatically. A specialist will follow up.")

    # happy (default): one inventory lookup, then a reply.
    if TOOL_CHECK_INVENTORY in available and TOOL_CHECK_INVENTORY not in called:
        return _tool_events(tid, TOOL_CHECK_INVENTORY, {"max_price": max_price})
    if TOOL_CHECK_FINANCING in available and TOOL_CHECK_INVENTORY not in available and TOOL_CHECK_FINANCING not in called:
        # Stage 0 and 1 register only the financing skill; answer through it.
        return _tool_events(tid, TOOL_CHECK_FINANCING, {"application_id": _APP_ID, "fail_attempts": finfail})
    return _text_events("Here are some options that fit your budget. Want me to book a viewing?")


class MockModel(Model):
    """A scripted decider that speaks the Strands streaming protocol."""

    def __init__(self, max_price: int = 15000) -> None:
        self._max_price = max_price

    # Model requires these three besides stream().
    def update_config(self, **model_config: Any) -> None:  # noqa: D401
        return None

    def get_config(self) -> Any:
        return {"provider": "mock"}

    def structured_output(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("mock model does not support structured output")

    async def stream(  # type: ignore[override]
        self,
        messages,
        tool_specs=None,
        system_prompt=None,
        **kwargs,
    ):
        # Latency lives in the mock so a presenter can Ctrl-C a model call. The
        # output never depends on time, so replay stays deterministic.
        await asyncio.sleep(config.model_latency_ms() / 1000.0)

        scenario, finfail, maxprice = _parse_directive(system_prompt)
        available = {s.get("name", "") for s in (tool_specs or [])}
        calls, status_by_tool, last_user_text = _summarize(list(messages))
        events = _decide(
            scenario,
            finfail,
            available,
            calls,
            status_by_tool,
            last_user_text,
            maxprice if maxprice is not None else self._max_price,
        )
        for event in events:
            yield event
