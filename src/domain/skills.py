"""The skills catalog as Temporal Activities.

Four skills: check_inventory, schedule_inspection, check_financing_status, and
send_customer_message. Each is a single @activity.defn used unchanged both as a
Workflow Activity (stage 2) and as a Standalone Activity (stage 1); that sameness
is part of the demo, so the definitions are not forked. Stage 0 reuses the pure
logic in backend.py directly.

Each skill carries its own Retry Policy, timeout, and non-retryable error list in
SKILL_OPTIONS, applied identically at the workflow call site and by stage 1's
client.execute_activity call.

Failure injection is a first-class demo affordance (the dashboard exposes a skill
failure count). A skill fails on attempt N while ``fail_attempts >= N``, using the
real Temporal attempt counter, so retries and exhaustion are genuine.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from domain import backend, config

# Error type raised for an input the backend can never satisfy. It is
# non-retryable: retrying a malformed request cannot help.
INVALID_REQUEST = "InvalidRequest"
# Error type raised for a transient backend failure. Retryable, and the one used
# to exhaust a fixed-count retry policy in the handoff scenario.
BACKEND_UNAVAILABLE = "BackendUnavailable"

# The skills take flat parameters, not a single dataclass, because activity_as_tool
# derives the agent tool's input schema directly from the Activity signature and
# the model calls the tool with flat named arguments. The same flat signature is
# used unchanged when stage 1 runs a skill as a Standalone Activity.


async def _dwell(skill: str) -> None:
    """Sleep the configured mock latency so a presenter can Ctrl-C mid call."""
    await asyncio.sleep(config.skill_latency_ms(skill) / 1000.0)


def _maybe_fail(skill: str, fail_attempts: int) -> None:
    """Raise a retryable failure while the current attempt is within the count."""
    attempt = activity.info().attempt
    if fail_attempts and attempt <= fail_attempts:
        raise ApplicationError(
            f"{skill} backend is temporarily unavailable (attempt {attempt})",
            type=BACKEND_UNAVAILABLE,
        )


@activity.defn
async def check_inventory(
    max_price: int | None = None, body_style: str | None = None, fail_attempts: int = 0
) -> dict:
    """Find vehicles in inventory that fit the customer's budget."""
    _maybe_fail("check_inventory", fail_attempts)
    await _dwell("check_inventory")
    vehicles = backend.find_inventory(max_price=max_price, body_style=body_style)
    return {"vehicles": vehicles, "count": len(vehicles)}


@activity.defn
async def schedule_inspection(vehicle_id: str = "", when: str = "", fail_attempts: int = 0) -> dict:
    """Book an inspection slot for a chosen vehicle."""
    if not vehicle_id:
        raise ApplicationError("vehicle_id is required", type=INVALID_REQUEST)
    _maybe_fail("schedule_inspection", fail_attempts)
    await _dwell("schedule_inspection")
    return backend.book_inspection(vehicle_id, when or "next available")


@activity.defn
async def check_financing_status(application_id: str = "", fail_attempts: int = 0) -> dict:
    """Look up the status of an existing financing application."""
    if not application_id:
        raise ApplicationError("application_id is required", type=INVALID_REQUEST)
    _maybe_fail("check_financing_status", fail_attempts)
    await _dwell("check_financing_status")
    return backend.financing_status(application_id)


@activity.defn
async def send_customer_message(
    text: str = "", conversation_id: str = "", fail_attempts: int = 0
) -> dict:
    """Deliver an outbound message to the customer through their channel.

    In mock mode the outbound URL points at the gateway's local stub, which the
    dashboard renders in the transcript. If no URL is configured (offline tests),
    the send is a no-op that still reports delivery, so the Workflow can record
    the outbound event without a live gateway.
    """
    _maybe_fail("send_customer_message", fail_attempts)
    await _dwell("send_customer_message")
    url = os.environ.get("OUTBOUND_URL", "").strip()
    delivered_via = "noop"
    if url:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                json={"conversation_id": conversation_id, "text": text},
            )
            resp.raise_for_status()
            delivered_via = url
    return {"delivered": True, "text": text, "via": delivered_via}


# All skill Activities, for worker registration.
ALL_SKILLS = [
    check_inventory,
    schedule_inspection,
    check_financing_status,
    send_customer_message,
]


@dataclass
class SkillOptions:
    """Per-skill Activity options, applied identically wherever the skill runs."""

    start_to_close_timeout: timedelta
    retry_policy: RetryPolicy
    non_retryable_error_types: list[str] = field(default_factory=list)


# A fixed, generous per-skill timeout. It must comfortably exceed the mock
# latency, and it is a constant rather than an env read so skill_options can be
# called inside a Workflow without breaking determinism.
_SKILL_TIMEOUT = timedelta(seconds=120)


def skill_options(skill: str) -> SkillOptions:
    """Return the Retry Policy, timeout, and non-retryable list for a skill."""
    if skill == "check_financing_status":
        # Fixed-count retries (beat 6): a small, bounded number of mechanical
        # retries, after which the exhausted failure is handed to the model.
        return SkillOptions(
            start_to_close_timeout=_SKILL_TIMEOUT,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=200),
                backoff_coefficient=1.5,
                maximum_attempts=3,
                non_retryable_error_types=[INVALID_REQUEST],
            ),
            non_retryable_error_types=[INVALID_REQUEST],
        )
    return SkillOptions(
        start_to_close_timeout=_SKILL_TIMEOUT,
        retry_policy=RetryPolicy(
            initial_interval=timedelta(milliseconds=200),
            backoff_coefficient=2.0,
            maximum_attempts=5,
            non_retryable_error_types=[INVALID_REQUEST],
        ),
        non_retryable_error_types=[INVALID_REQUEST],
    )
