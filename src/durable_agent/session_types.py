"""The Workflow input type, kept free of heavy imports.

The session Workflow imports this, so it must not pull in modules that do IO at
import time (scenario loading, model construction). Only this small dataclass
lives here; everything that resolves it lives in config.py on the client side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The default follow-up delay in seconds (three days), duplicated here as a
# plain constant so this module needs no import-time work.
_DEFAULT_FOLLOW_UP_SECONDS = 3 * 24 * 60 * 60


@dataclass
class SessionInput:
    """Everything the session Workflow needs, resolved so the Workflow does no IO."""

    system_prompt: str
    model: str = "mock"
    scenario: str = "happy"
    conversation_id: str = ""
    # How many times the financing skill fails before succeeding. Carried in the
    # session config, not injected by the model, so beat 6 fails deterministically
    # with any model (the mock and a real provider alike).
    fail_count: int = 0
    follow_up_seconds: int = _DEFAULT_FOLLOW_UP_SECONDS
    # Carried across a Continue-As-New boundary (beat 8). Strands message dicts.
    messages: list[Any] = field(default_factory=list)
    # An absolute follow-up deadline carried across Continue-As-New as an epoch
    # second, so a delay at the boundary cannot shift it (beat 5 plus beat 8).
    follow_up_deadline_epoch: float | None = None
    # Inbound messages not yet handled, carried across the boundary so none are
    # dropped when the run rolls over.
    pending: list[str] = field(default_factory=list)
    # Publish the armed follow-up deadline as the NextFollowUpAt Search Attribute, so
    # planned wake-ups across sessions are one list filter in the UI. Off unless the
    # client turns it on, because the attribute must be registered on the namespace.
    track_follow_up: bool = False
