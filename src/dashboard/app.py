"""Dashboard backend: dumb proxy to Temporal, the gateway, and the harness feed.

No business logic. It calls start, signal, query, and describe, and reads the
stage 0 and stage 1 local feed. The lane divergence that the demo turns on is a
property of Temporal, not of this code: describe is answered by the server and
survives Worker death, while a Workflow query is answered by the Worker and fails
when it is down. The dashboard simply reports which succeeded.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from domain.config import TASK_QUEUE_AGENT
from durable_agent import config
from durable_agent.workflows import SessionWorkflow

_HTML = Path(__file__).resolve().parent / "index.html"

# Where the send control and the local feeds live. Stage 2 sends through the
# gateway; stages 0 and 1 send to and read from the harness feed, because there
# is no Workflow to signal-with-start yet.
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8090")
HARNESS_URL = {
    "0": os.environ.get("HARNESS0_URL", "http://127.0.0.1:8091"),
    "1": os.environ.get("HARNESS1_URL", "http://127.0.0.1:8092"),
}

_QUERY_TIMEOUT = timedelta(seconds=2)


class SendIn(BaseModel):
    stage: str = "2"
    conversation: str
    text: str
    scenario: str | None = None
    fail_count: int | None = None


class ControlIn(BaseModel):
    conversation: str
    action: str
    text: str = ""
    decision: str = "approved"
    approver: str = "back office"


def create_app() -> FastAPI:
    app = FastAPI(title="durable agent demo dashboard")
    state: dict[str, Any] = {"client": None}
    # Last successful worker-answered transcript and time, per conversation, so a
    # frozen view can be shown (dimmed) while the Worker is down.
    last_good: dict[str, dict[str, Any]] = {}

    async def client() -> Any:
        if state["client"] is None:
            state["client"] = await config.connect_client()
        return state["client"]

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_HTML)

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        from domain import config as dconfig

        return {
            "model_mode": config.model_mode(),
            "skills_mode": config.skills_mode(),
            "temporal_target": config.temporal_target(),
            "model_queue_rps": dconfig.model_queue_rps(),
            "follow_up_seconds": dconfig.follow_up_seconds(),
            # "agentcore" switches the WORKER lane to the serverless drain framing
            # (DRAINED / compute reaped) and shows the worker identity as location.
            "worker_mode": os.environ.get("WORKER_MODE", "local"),
        }

    @app.get("/api/state")
    async def api_state(stage: str = "2", conversation: str = "demo") -> dict[str, Any]:
        if stage in ("0", "1"):
            return await _harness_state(stage)
        return await _workflow_state(conversation)

    async def _harness_state(stage: str) -> dict[str, Any]:
        url = HARNESS_URL[stage]
        try:
            async with httpx.AsyncClient(timeout=2.0) as http:
                resp = await http.get(f"{url}/feed")
                resp.raise_for_status()
                feed = resp.json()
            return {
                "stage": stage,
                "transcript": feed["transcript"],
                "feed_alive": True,
                # Stages 0 and 1 have no durable server-side state. The lanes are
                # dead on purpose, and that is the point.
                "platform": {"mode": "dead", "label": "no durable state"},
                "worker": {
                    "mode": "dead" if stage == "0" else "na",
                    "label": "process is the state" if stage == "0" else "harness is not a worker",
                },
            }
        except (httpx.HTTPError, httpx.ConnectError):
            # The harness process died: its feed stops answering, so the
            # transcript is lost, not stale.
            return {
                "stage": stage,
                "transcript": [],
                "feed_alive": False,
                "platform": {"mode": "dead", "label": "no durable state"},
                "worker": {"mode": "dead", "label": "process is the state"},
            }

    async def _workflow_state(conversation: str) -> dict[str, Any]:
        cl = await client()
        handle = cl.get_workflow_handle(conversation)

        # PLATFORM lane: describe, answered by the server. Survives Worker death.
        platform: dict[str, Any] = {"mode": "durable"}
        try:
            desc = await handle.describe()
            status_name = desc.status.name if desc.status is not None else "UNKNOWN"
            platform.update({"exists": True, "status": status_name})
        except Exception:  # noqa: BLE001
            platform.update({"exists": False, "status": "NOT_STARTED"})

        # WORKER lane: a Workflow query, answered by the Worker. Fails when down.
        # The query is the crisp drain signal: it flips within the query timeout
        # (about 2s) when the Worker is gone, faster than poller visibility ages out.
        worker: dict[str, Any] = {}
        transcript: list[dict] = []
        try:
            status = await handle.query(SessionWorkflow.status, rpc_timeout=_QUERY_TIMEOUT)
            transcript = await handle.query(SessionWorkflow.transcript, rpc_timeout=_QUERY_TIMEOUT)
            last_good[conversation] = {"transcript": transcript, "at": _now_iso()}
            worker = {"online": True, "status": status, "last_query_at": last_good[conversation]["at"]}
        except Exception:  # noqa: BLE001
            cached = last_good.get(conversation, {})
            transcript = cached.get("transcript", [])
            worker = {"online": False, "last_query_at": cached.get("at")}

        # Enrich with where the Worker runs and how many are polling. This is what makes
        # "the Worker is in AgentCore" and "it drained (0 pollers)" concrete on screen.
        worker.update(await _pollers(cl))

        return {"stage": "2", "platform": platform, "worker": worker, "transcript": transcript}

    async def _pollers(cl: Any) -> dict[str, Any]:
        """Poller count and identities on the agent-session queue, via DescribeTaskQueue.

        Server-side, so it answers even with no Worker. Used for the WORKER lane detail:
        identity proves location (for example agentcore-runtime), and a count of 0
        confirms the compute drained rather than merely failing to answer.
        """
        try:
            from temporalio.api.enums.v1 import TaskQueueType
            from temporalio.api.taskqueue.v1 import TaskQueue
            from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

            resp = await cl.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=cl.namespace,
                    task_queue=TaskQueue(name=TASK_QUEUE_AGENT),
                    task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
                )
            )
            identities = sorted({p.identity for p in resp.pollers if p.identity})
            return {"pollers": len(resp.pollers), "identities": identities}
        except Exception:  # noqa: BLE001
            return {"pollers": None, "identities": []}

    @app.post("/api/send")
    async def api_send(body: SendIn) -> dict[str, Any]:
        if body.stage in ("0", "1"):
            url = HARNESS_URL[body.stage]
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(
                    f"{url}/message",
                    json={
                        "session_id": body.conversation,
                        "text": body.text,
                        "scenario": body.scenario or "handoff",
                        "fail_count": body.fail_count or 0,
                    },
                )
                return {"sent_to": "harness", "status": resp.status_code}
        # Stage 2: always through the gateway, never the Temporal client directly.
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(
                f"{GATEWAY_URL}/inbound",
                json={
                    "channel": "dashboard",
                    "conversation": body.conversation,
                    "text": body.text,
                    "scenario": body.scenario,
                    "fail_count": body.fail_count,
                },
            )
            resp.raise_for_status()
            return {"sent_to": "gateway", **resp.json()}

    @app.post("/api/control")
    async def api_control(body: ControlIn) -> dict[str, Any]:
        cl = await client()
        handle = cl.get_workflow_handle(body.conversation)
        if body.action == "customer_replies_now":
            await handle.signal(SessionWorkflow.customer_replies_now, body.text or "I'm ready now")
        elif body.action == "fast_forward_follow_up":
            await handle.signal(SessionWorkflow.fast_forward_follow_up)
        elif body.action == "respond_handoff":
            await handle.signal(
                SessionWorkflow.respond_handoff, args=[body.decision, body.approver]
            )
        elif body.action == "force_continue_as_new":
            await handle.signal(SessionWorkflow.force_continue_as_new)
        else:
            return {"ok": False, "error": f"unknown action {body.action}"}
        return {"ok": True, "action": body.action}

    return app


def _now_iso() -> str:
    # Wall-clock timestamp for the last successful query. This is display only and
    # never feeds Workflow logic, so a plain clock read is fine here.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%H:%M:%S")


app = create_app()
