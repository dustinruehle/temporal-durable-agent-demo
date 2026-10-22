"""The channel gateway app.

Inbound is a channel-agnostic webhook that performs Signal-With-Start with the
conversation id as the Workflow Id. There is no exists-check, no lookup table, and
no lock: the Temporal Service resolves the race. The dashboard's send control and a
presenter's curl both traverse this exact handler.

Outbound is a small local stub the send_customer_message Activity POSTs to in mock
mode, so beat 5's follow-up leaves through the same channel a customer would
receive it on rather than appearing by magic in a UI.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from temporalio.client import Client

from domain.scenarios import classify
from durable_agent.session_client import signal_with_start


class InboundMessage(BaseModel):
    # Channel-agnostic shape: a channel name, a channel-native conversation id,
    # and message text. Scenario and fail_count are demo controls, both optional.
    channel: str = "demo"
    conversation: str
    text: str
    scenario: str | None = None
    # None means use the scenario's own failure counts; a value overrides them.
    fail_count: int | None = None


class OutboundMessage(BaseModel):
    conversation_id: str = ""
    text: str


def make_app(client: Client) -> FastAPI:
    app = FastAPI(title="channel gateway")
    outbox: list[dict[str, Any]] = []

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/inbound")
    async def inbound(msg: InboundMessage) -> dict[str, str]:
        # Map the conversation id to a Workflow Id and Signal-With-Start. The
        # scenario is chosen explicitly by the dashboard, or classified from the
        # text for a bare curl. No exists-check and no lock around this call.
        scenario = msg.scenario or classify(msg.text)
        handle = await signal_with_start(
            client, msg.conversation, msg.text, scenario=scenario, fail_count=msg.fail_count
        )
        return {
            "workflow_id": handle.id,
            "run_id": handle.first_execution_run_id or handle.result_run_id or "",
            "scenario": scenario,
        }

    @app.post("/outbound")
    def outbound(msg: OutboundMessage) -> dict[str, Any]:
        # The local outbound stub. In live mode this URL points at a real channel.
        outbox.append({"conversation_id": msg.conversation_id, "text": msg.text})
        return {"delivered": True, "count": len(outbox)}

    @app.get("/outbound")
    def outbound_log() -> dict[str, Any]:
        return {"outbox": outbox}

    return app
