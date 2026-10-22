"""The AgentCore serverless-worker runtime drives the same outcomes.

The runtime holds no persistent Worker. Each invocation spins an ephemeral Worker:
an empty payload polls and drains (the Serverless Workers / WCI path), and a payload
with a message runs that Worker, delivers the turn through Signal-With-Start, and
returns the reply. The runtime session id is the Workflow Id, so a later invocation
resumes the same conversation, which is what lets a session outlive a microVM.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
from temporalio.client import Client

from durable_agent.config import build_plugin
from durable_agent.session_client import signal_with_start
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from runtime.app import HEALTHY, SESSION_HEADER, make_app


def _session_id() -> str:
    # AgentCore requires at least 33 characters.
    sid = f"agentcore-{uuid4().hex}-{uuid4().hex}"
    assert len(sid) >= 33
    return sid


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runtime")


def _app(client: Client):
    return make_app(client, build_plugin("mock"))


async def _query(client: Client, sid: str, query, *args):
    # In the serverless model there is no persistent Worker, and a Workflow Query
    # needs a Worker to answer it. Spin a short-lived one to read state, the same way
    # the dashboard would need a Worker polling to show its live query lane.
    async with running_workers(client, build_plugin("mock")):
        return await client.get_workflow_handle(sid).query(query, *args)


async def test_ping_reports_healthy_when_idle(client: Client):
    async with _http(_app(client)) as http:
        r = await http.get("/ping")
        assert r.status_code == 200
        # Idle (no invocation in flight) reports Healthy, so AgentCore can reap it.
        assert r.json()["status"] == HEALTHY


async def test_invocations_requires_a_long_session_id(client: Client):
    async with _http(_app(client)) as http:
        r = await http.post(
            "/invocations", headers={SESSION_HEADER: "too-short"}, json={"prompt": "hi"}
        )
        assert r.status_code == 400


async def test_invocations_rejects_a_non_json_body_cleanly(client: Client):
    # A malformed or non-utf8 body must return a clean 400, not a 500. The AgentCore
    # SDK parses the body and returns a clean 400 on bad JSON or a non-utf8 payload.
    async with _http(_app(client)) as http:
        r = await http.post(
            "/invocations",
            headers={SESSION_HEADER: _session_id(), "content-type": "application/octet-stream"},
            content=b"\xb5\xec\x6d\x21\xa9",
        )
        assert r.status_code == 400
        assert "error" in r.json()


async def test_message_invocation_boots_an_ephemeral_worker_and_replies(client: Client):
    # No external Worker: the invocation itself provides the Worker, processes the
    # turn, and returns the reply. Body without a JSON content type still parses.
    sid = _session_id()
    async with _http(_app(client)) as http:
        r = await http.post(
            "/invocations",
            headers={SESSION_HEADER: sid, "content-type": "application/octet-stream"},
            content=json.dumps({"prompt": "I'm looking to buy under 15k", "scenario": "happy"}).encode(),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "replied"
        assert r.json()["response"].strip() != ""
        await client.get_workflow_handle(sid).signal(SessionWorkflow.close_session)


async def test_session_resumes_across_invocations(client: Client):
    sid = _session_id()
    async with _http(_app(client)) as http:
        r = await http.post(
            "/invocations", headers={SESSION_HEADER: sid},
            json={"prompt": "I'm looking to buy under 15k", "scenario": "happy"},
        )
        assert r.json()["status"] == "replied"
        # A later invocation on the same session id (a fresh ephemeral Worker) resumes
        # the same Workflow.
        r2 = await http.post(
            "/invocations", headers={SESSION_HEADER: sid},
            json={"prompt": "any SUVs?", "scenario": "happy"},
        )
        assert r2.json()["status"] == "replied"
    status = await _query(client, sid, SessionWorkflow.status)
    assert status["processed"] == 2
    await client.get_workflow_handle(sid).signal(SessionWorkflow.close_session)


async def test_empty_payload_drains_a_queued_workflow(client: Client):
    # The Serverless Workers path: a turn is queued with no Worker running; an empty
    # invocation boots an ephemeral Worker that drains it and then returns.
    sid = _session_id()
    await signal_with_start(client, sid, "I'm looking to buy under 15k", "happy")
    async with _http(_app(client)) as http:
        r = await http.post("/invocations", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "drained"
    status = await _query(client, sid, SessionWorkflow.status)
    assert status["processed"] >= 1
    await client.get_workflow_handle(sid).signal(SessionWorkflow.close_session)


async def test_beat5_invocation_arms_durable_follow_up(client: Client):
    sid = _session_id()
    async with _http(_app(client)) as http:
        r = await http.post(
            "/invocations", headers={SESSION_HEADER: sid},
            json={"prompt": "call me later", "scenario": "deferred_follow_up"},
        )
        assert r.json()["status"] == "replied"
    # The follow-up is armed and durable in Temporal, so the session can end.
    status = await _query(client, sid, SessionWorkflow.status)
    assert status["follow_up_armed"] is True
    await client.get_workflow_handle(sid).signal(SessionWorkflow.close_session)


async def test_beat6_hands_off_then_resumes_on_next_invocation(client: Client):
    sid = _session_id()
    async with _http(_app(client)) as http:
        r = await http.post(
            "/invocations", headers={SESSION_HEADER: sid},
            json={"prompt": "financing please", "scenario": "handoff"},
        )
        # The turn parks on the back-office handoff rather than replying.
        assert r.json()["status"] == "handed_off"
        assert r.json()["pending_handoff"]

        # The back office responds out of band; the next invocation boots a Worker
        # that resumes the parked handoff. This is the Serverless Workers flow: the
        # respond signal queues a Task, Temporal invokes the runtime again.
        await client.get_workflow_handle(sid).signal(
            SessionWorkflow.respond_handoff, args=["approved", "back office"]
        )
        r2 = await http.post("/invocations", json={})
        assert r2.status_code == 200

    transcript = await _query(client, sid, SessionWorkflow.transcript)
    calls = [e["tool"] for e in transcript if e["kind"] == "tool_call"]
    assert calls == ["check_financing_status", "check_inventory", "hand_off_to_back_office"]
    assert any(e["kind"] == "agent_reply" for e in transcript)
    await client.get_workflow_handle(sid).signal(SessionWorkflow.close_session)
