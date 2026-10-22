"""Planned wake-ups across sessions: the armed follow-up is published as NextFollowUpAt.

Every session that has a follow-up armed carries its deadline in the NextFollowUpAt
Search Attribute, so one list filter shows the planned work across all sessions. The
attribute follows the deadline: set when armed, unset when it fires or is cancelled.
Runs on the real local dev server, which registers the attribute like run.sh does.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from durable_agent.config import build_plugin
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from durable_agent.workflows.session import NEXT_FOLLOW_UP_AT
from tests.offline._harness import (
    new_conversation_id,
    start_session,
    wait_for_processed,
    wait_for_transcript_kind,
)

_ONE_HOUR = 60 * 60


@pytest.fixture(autouse=True)
def _publish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOLLOW_UP_SEARCH_ATTRIBUTE", "on")


async def _next_follow_up(handle):
    return (await handle.describe()).typed_search_attributes.get(NEXT_FOLLOW_UP_AT)


async def _wait_attr(handle, pred, timeout: float = 15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        value = await _next_follow_up(handle)
        if pred(value):
            return value
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"NextFollowUpAt stayed {value!r}")
        await asyncio.sleep(0.1)


async def test_armed_follow_up_is_listed_then_cancel_clears_it(client):
    conv = new_conversation_id()
    async with running_workers(client, build_plugin("mock")):
        handle = await start_session(
            client, "deferred_follow_up", conv, follow_up_seconds=_ONE_HOUR
        )
        await handle.signal(SessionWorkflow.deliver_message, "I'll think about it, call me later")
        await wait_for_processed(handle, 1)
        at = await _wait_attr(handle, lambda v: v is not None)
        assert abs(at.timestamp() - (time.time() + _ONE_HOUR)) < 120

        # The planned-tasks view: one query across sessions.
        query = f'NextFollowUpAt IS NOT NULL AND WorkflowId = "{conv}"'
        await asyncio.sleep(1)  # visibility is eventually consistent
        listed = [wf.id async for wf in client.list_workflows(query)]
        assert listed == [conv]

        await handle.signal(SessionWorkflow.customer_replies_now, "the SUV please")
        await _wait_attr(handle, lambda v: v is None)
        await handle.signal(SessionWorkflow.close_session)


async def test_fired_follow_up_clears_it_and_carries_the_conversation_id(client):
    conv = new_conversation_id()
    async with running_workers(client, build_plugin("mock")):
        handle = await start_session(
            client, "deferred_follow_up", conv, follow_up_seconds=_ONE_HOUR
        )
        await handle.signal(SessionWorkflow.deliver_message, "I'll think about it, call me later")
        await wait_for_processed(handle, 1)
        await _wait_attr(handle, lambda v: v is not None)
        await handle.signal(SessionWorkflow.fast_forward_follow_up)
        await wait_for_transcript_kind(handle, "outbound")
        await _wait_attr(handle, lambda v: v is None)

        # The outbound send carries the session's conversation id, not one the model chose.
        sends = []
        async for event in handle.fetch_history_events():
            if event.HasField("activity_task_scheduled_event_attributes"):
                attrs = event.activity_task_scheduled_event_attributes
                if attrs.activity_type.name == "send_customer_message":
                    sends.append([p.data.decode() for p in attrs.input.payloads])
        assert sends and f'"{conv}"' in sends[0]
        await handle.signal(SessionWorkflow.close_session)


async def test_off_by_default_publishes_nothing(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FOLLOW_UP_SEARCH_ATTRIBUTE", "off")
    conv = new_conversation_id()
    async with running_workers(client, build_plugin("mock")):
        handle = await start_session(
            client, "deferred_follow_up", conv, follow_up_seconds=_ONE_HOUR
        )
        await handle.signal(SessionWorkflow.deliver_message, "I'll think about it, call me later")
        await wait_for_processed(handle, 1)
        assert (await handle.query(SessionWorkflow.status))["follow_up_armed"] is True
        assert await _next_follow_up(handle) is None
        await handle.signal(SessionWorkflow.close_session)
