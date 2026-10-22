"""The agent system prompt and the internal markers the mock model reads.

The prompt is ordinary human-facing instruction text a real provider follows.
The mock model needs to know which scenario is running and how many times the
financing check should fail; that is carried in an HTML comment directive a real
model harmlessly ignores. The follow-up-due signal is carried as a marker on the
re-invocation message, not in the system prompt.
"""

from __future__ import annotations

from domain.markers import FOLLOW_UP_DUE_MARKER, HANDOFF_RESOLVED_MARKER
from domain.scenarios import Scenario

__all__ = [
    "FOLLOW_UP_DUE_MARKER",
    "HANDOFF_RESOLVED_MARKER",
    "build_system_prompt",
    "sim_directive",
]

_BASE_PROMPT = """\
You are the assistant for a vehicle marketplace. A customer messages you about
buying or selling a vehicle, and you follow the whole journey with them.

Guidance:
- Use check_inventory to find vehicles that fit the customer's budget.
- Use schedule_inspection to book a viewing once a vehicle is chosen.
- Use check_financing_status to look up an existing financing application.
- Use send_customer_message to reach the customer through their channel.
- When a customer is not ready to decide now, use schedule_follow_up to record a
  reminder rather than ending the conversation.
- If check_financing_status fails or returns an error, do not apologize and stop.
  First call check_inventory to look for a lower-priced vehicle the customer could
  buy without that financing. If check_inventory still does not resolve the need,
  call hand_off_to_back_office so a human can take over. Only send a plain text
  reply once you have tried these options or the need is resolved.
Keep replies short and concrete.\
"""


def sim_directive(scenario: Scenario, financing_fail_override: int | None = None) -> str:
    """The machine directive the mock parses. A real model ignores the comment.

    The financing fail count comes from the scenario profile unless the caller
    overrides it, which is how the dashboard skill-failure-count control feeds
    beats 2 and 6 without editing a dataset file.
    """
    fin = (
        scenario.financing_fail_count()
        if financing_fail_override is None
        else int(financing_fail_override)
    )
    return f"<!--sim scenario={scenario.name} finfail={fin} maxprice={scenario.max_price}-->"


def build_system_prompt(scenario: Scenario, financing_fail_override: int | None = None) -> str:
    return f"{_BASE_PROMPT}\n\n{sim_directive(scenario, financing_fail_override)}"
