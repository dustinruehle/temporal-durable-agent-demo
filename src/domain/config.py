"""Shared, env-driven tunables read at call time.

These are read at call time rather than import time so tests can set them to
near zero without reimporting, and so run.sh can print the resolved values. The
delays live inside the mock so they exist identically in stage 0, stage 1, and
stage 2, and so a real provider simply supplies its own latency instead.
"""

from __future__ import annotations

import os
from datetime import timedelta

# Defaults long enough for a comfortable Ctrl-C during a live kill, short enough
# that the offline suite stays fast once tests override them to near zero.
_DEFAULT_SKILL_LATENCY_MS = 4000
_DEFAULT_MODEL_LATENCY_MS = 2500
# The default follow-up delay. Beat 5 overrides it to seconds in tests. Never
# special-case demo mode inside Workflow code; change this value instead.
_DEFAULT_FOLLOW_UP_SECONDS = 3 * 24 * 60 * 60  # three days
# High enough to be invisible during beats 3 to 6. Beat 7 lowers it.
_DEFAULT_MODEL_QUEUE_RPS = 1000.0


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def skill_latency_ms(skill: str | None = None) -> int:
    """Configured mock skill latency, with an optional per-skill override."""
    if skill:
        override = os.environ.get(f"MOCK_SKILL_LATENCY_MS__{skill}")
        if override:
            return _int_env(f"MOCK_SKILL_LATENCY_MS__{skill}", _DEFAULT_SKILL_LATENCY_MS)
    return _int_env("MOCK_SKILL_LATENCY_MS", _DEFAULT_SKILL_LATENCY_MS)


def model_latency_ms() -> int:
    return _int_env("MOCK_MODEL_LATENCY_MS", _DEFAULT_MODEL_LATENCY_MS)


def follow_up_seconds() -> int:
    return _int_env("FOLLOW_UP_DELAY", _DEFAULT_FOLLOW_UP_SECONDS)


def follow_up_search_attribute() -> bool:
    """Whether sessions publish NextFollowUpAt. The attribute must be registered."""
    return os.environ.get("FOLLOW_UP_SEARCH_ATTRIBUTE", "off").strip().lower() in ("on", "1", "true")


def model_queue_rps() -> float:
    return _float_env("MODEL_QUEUE_RPS", _DEFAULT_MODEL_QUEUE_RPS)


# Task queues. agent-session hosts the session Workflow and
# the skill Activities and stage 1's Standalone Activity. model-calls hosts model
# Activities in every beat, so the architecture is constant and beat 7 is a
# configuration change, not a structural one.
#
# The names can be overridden with TASK_QUEUE_AGENT and TASK_QUEUE_MODEL, so two
# copies of the demo can share one Namespace without either Worker pool picking up
# the other's tasks. They are read once, when the Worker process imports this
# module (outside the Workflow sandbox), so every Workflow Task in that process sees
# the same values. Every process in one demo (Worker, gateway, dashboard, scripts)
# must see the same values, which run.sh arranges by loading .env.
TASK_QUEUE_AGENT = os.environ.get("TASK_QUEUE_AGENT", "").strip() or "agent-session"
TASK_QUEUE_MODEL = os.environ.get("TASK_QUEUE_MODEL", "").strip() or "model-calls"

# The Standalone Activity id used by stage 1, known ahead of time because the
# demo starts the Activity itself.
STAGE1_ACTIVITY_ID = "stage1-financing-check"
# Timeout for stage 1's Standalone Activity call.
STAGE1_ACTIVITY_TIMEOUT = timedelta(seconds=120)
