"""Transcript event types shared by all three stages.

The dashboard renders one conversation view regardless of stage, so every stage
emits the same event shape. Stages 0 and 1 hold these in memory and expose them
over a tiny local feed; stage 2 returns them from a Workflow query. The type is a
plain dataclass so it serializes cleanly through both paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Event kinds. Kept as plain strings so the dashboard can switch on them without
# importing this module.
CUSTOMER_MESSAGE = "customer_message"  # inbound text from the customer
AGENT_REPLY = "agent_reply"  # the assistant's text reply
TOOL_CALL = "tool_call"  # a skill or tool the agent invoked
TOOL_RESULT = "tool_result"  # the outcome of a tool call
SYSTEM = "system"  # durable lifecycle notes, e.g. follow-up armed, handed off
OUTBOUND = "outbound"  # a message that left through the channel gateway


@dataclass
class TranscriptEvent:
    """One line in the conversation transcript."""

    kind: str
    text: str
    # Monotonic sequence assigned by the producer, so consumers can order and
    # de-duplicate without relying on wall-clock time.
    seq: int = 0
    tool: str | None = None
    # ok | failed | retrying | pending, for TOOL_RESULT and TOOL_CALL.
    status: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "seq": self.seq,
            "tool": self.tool,
            "status": self.status,
            "detail": self.detail,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> TranscriptEvent:
        return TranscriptEvent(
            kind=d["kind"],
            text=d["text"],
            seq=d.get("seq", 0),
            tool=d.get("tool"),
            status=d.get("status"),
            detail=d.get("detail", {}),
        )
