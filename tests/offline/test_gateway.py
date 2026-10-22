"""The channel gateway performs Signal-With-Start correctly.

This is beat 3 at the gateway boundary: a POST to /inbound starts or signals the
session, the same way a presenter's curl and the dashboard button do, because they
are the same handler.
"""

from __future__ import annotations

import asyncio

import httpx
from temporalio.client import Client

from channel.gateway import make_app
from durable_agent.config import build_plugin
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from tests.offline._harness import close_and_result, new_conversation_id, wait_for_processed


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


async def test_inbound_starts_then_signals_one_execution(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    app = make_app(client)
    async with running_workers(client, plugin), _http(app) as http:
        r1 = await http.post("/inbound", json={"conversation": conv, "text": "buying under 15k"})
        assert r1.status_code == 200
        assert r1.json()["workflow_id"] == conv
        handle = client.get_workflow_handle(conv)
        await wait_for_processed(handle, 1)

        # A second inbound for the same conversation signals the same execution.
        r2 = await http.post("/inbound", json={"conversation": conv, "text": "any SUVs?"})
        assert r2.json()["workflow_id"] == conv
        await wait_for_processed(handle, 2)

        result = await close_and_result(client, conv, handle)

    assert result["processed"] == 2


async def test_inbound_classifies_scenario_from_text(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    app = make_app(client)
    async with running_workers(client, plugin), _http(app) as http:
        # "financing" routes to the handoff scenario with no explicit scenario given.
        r = await http.post("/inbound", json={"conversation": conv, "text": "I need financing"})
        assert r.json()["scenario"] == "handoff"
        handle = client.get_workflow_handle(conv)
        # It parks on the handoff signal; confirm it reached that point.
        deadline = asyncio.get_event_loop().time() + 20
        while (await handle.query(SessionWorkflow.pending_handoff)) is None:
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.05)
        await handle.signal(SessionWorkflow.respond_handoff, args=["approved", "bo"])
        await wait_for_processed(handle, 1)
        await close_and_result(client, conv, handle)
