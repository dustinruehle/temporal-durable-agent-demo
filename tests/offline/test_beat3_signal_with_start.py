"""Beat 3: an inbound message starts or signals the session.

Workflow Id is the conversation id. No exists-check, no rehydrate path. The same
signal-with-start resolves the start case and the already-running case, and two
concurrent inbound messages for one conversation id resolve to one execution.
"""

from __future__ import annotations

import asyncio

from temporalio.client import Client

from durable_agent.config import build_plugin
from durable_agent.session_client import signal_with_start
from durable_agent.worker import running_workers
from tests.offline._harness import (
    close_and_result,
    new_conversation_id,
    wait_for_processed,
)


async def test_start_then_signal_is_one_execution(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        h1 = await signal_with_start(client, conv, "hi, I'm buying under 15k", "happy")
        await wait_for_processed(h1, 1)

        # Already running: a second inbound signals the same execution.
        h2 = await signal_with_start(client, conv, "any SUVs?", "happy")
        assert h2.first_execution_run_id == h1.first_execution_run_id
        await wait_for_processed(h1, 2)

        result = await close_and_result(client, conv, h1)

    assert result["processed"] == 2
    customer_msgs = [e for e in result["transcript"] if e["kind"] == "customer_message"]
    assert len(customer_msgs) == 2


async def test_two_concurrent_inbound_resolve_to_one_execution(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        h1, h2 = await asyncio.gather(
            signal_with_start(client, conv, "message one", "happy"),
            signal_with_start(client, conv, "message two", "happy"),
        )
        # Both handles point at the same first execution.
        assert h1.first_execution_run_id == h2.first_execution_run_id
        await wait_for_processed(h1, 2)

        result = await close_and_result(client, conv, h1)

    assert result["processed"] == 2
