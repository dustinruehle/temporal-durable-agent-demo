"""Shared helpers for stage 2 Workflow tests."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from temporalio.client import Client, WorkflowHandle

from domain.config import TASK_QUEUE_AGENT
from durable_agent.config import build_plugin, build_session_input
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow


def new_conversation_id(prefix: str = "conv") -> str:
    return f"{prefix}-{uuid4()}"


def tool_calls(transcript: list[dict]) -> list[str]:
    return [e["tool"] for e in transcript if e["kind"] == "tool_call"]


def tool_results(transcript: list[dict]) -> list[tuple[str, str]]:
    return [(e["tool"], e["status"]) for e in transcript if e["kind"] == "tool_result"]


def kinds(transcript: list[dict]) -> list[str]:
    return [e["kind"] for e in transcript]


async def wait_for_processed(handle: WorkflowHandle, n: int, timeout: float = 40.0) -> dict:
    """Poll the status query until at least n messages have been processed."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        status = await handle.query(SessionWorkflow.status)
        if status["processed"] >= n:
            return status
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"only {status['processed']}/{n} processed before timeout")
        await asyncio.sleep(0.05)


async def close_and_result(client: Client, conversation_id: str, handle: WorkflowHandle) -> dict:
    await client.get_workflow_handle(conversation_id).signal(SessionWorkflow.close_session)
    return await handle.result()


async def wait_for_new_run(handle: WorkflowHandle, old_run_id: str, timeout: float = 15.0) -> str:
    """Poll until the workflow id rolls over to a new run (Continue-As-New)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        run_id = (await handle.describe()).run_id
        if run_id != old_run_id:
            return run_id
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("no Continue-As-New before timeout")
        await asyncio.sleep(0.05)


async def wait_for_transcript_kind(
    handle: WorkflowHandle, kind: str, timeout: float = 15.0
) -> list[dict]:
    """Poll the transcript query until an event of the given kind appears."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        transcript = await handle.query(SessionWorkflow.transcript)
        if any(e["kind"] == kind for e in transcript):
            return transcript
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"no '{kind}' event before timeout")
        await asyncio.sleep(0.05)


async def start_session(
    client: Client,
    scenario: str,
    conversation_id: str,
    fail_count: int | None = None,
    follow_up_seconds: int | None = None,
) -> WorkflowHandle:
    inp = build_session_input(scenario, fail_count=fail_count, conversation_id=conversation_id)
    if follow_up_seconds is not None:
        inp.follow_up_seconds = follow_up_seconds
    return await client.start_workflow(
        SessionWorkflow.run,
        inp,
        id=conversation_id,
        task_queue=TASK_QUEUE_AGENT,
    )


async def run_one_message(
    client: Client,
    scenario: str,
    message: str,
    fail_count: int | None = None,
) -> dict:
    """Start a session, deliver one message, close, and return the result dict."""
    plugin = build_plugin("mock")
    conversation_id = new_conversation_id()
    async with running_workers(client, plugin):
        handle = await start_session(client, scenario, conversation_id, fail_count=fail_count)
        await handle.signal(SessionWorkflow.deliver_message, message)
        # Wait for the turn to be processed before closing, otherwise close_session can
        # win the race and the Workflow exits with nothing processed.
        await wait_for_processed(handle, 1)
        await handle.signal(SessionWorkflow.close_session)
        return await handle.result()
