"""Prove the Serverless Worker path: Temporal invokes the runtime, no local Worker.

Run this with NO Worker running and WITHOUT calling InvokeAgentRuntime yourself. It
signals-with-start a fresh session on the agent-session task queue and then waits. If
the Worker Deployment Version is wired with the AgentCore compute provider, Temporal's
Worker Controller sees a task with no poller, assumes the invoke role, calls
InvokeAgentRuntime, and a Worker boots inside AgentCore, runs the turn, and drains.

Success is a reply coming back with nothing local polling. The script also reports the
Worker Controller Instance workflow and the task-queue pollers as they appear, so you
can see who started the Worker.

Usage:
  uv run python scripts/verify_serverless.py ["your message"]
  uv run python scripts/verify_serverless.py --follow-up 90

--follow-up SECONDS proves a durable follow-up across scale to zero: the turn arms a
follow-up, the AgentCore Worker drains, nothing runs, and when the Timer fires Temporal
starts a Worker again to send it. It reads Event History only, because a Query needs a
live Worker and there is none between the two.

Reads the Temporal Cloud connection and MODEL_MODE from the repo .env.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, "src")

from domain.config import TASK_QUEUE_AGENT  # noqa: E402
from durable_agent import config  # noqa: E402
from durable_agent.session_client import signal_with_start  # noqa: E402
from durable_agent.workflows import SessionWorkflow  # noqa: E402

TIMEOUT = 240.0
WCI_QUERY = 'TemporalNamespaceDivision = "TemporalWorkerControllerInstance"'


async def _replies(handle) -> list[dict]:
    try:
        transcript = await handle.query(SessionWorkflow.transcript)
    except Exception:  # noqa: BLE001  (no poller yet -> query fails; keep waiting)
        return []
    return [e for e in transcript if e["kind"] == "agent_reply"]


async def _wci_seen(client) -> bool:
    try:
        count = 0
        async for _ in client.list_workflows(WCI_QUERY):
            count += 1
            if count:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


FOLLOW_UP_MESSAGE = (
    "I like the cars under 15k but I need some time to think it over. "
    "Please schedule a follow-up and check back with me later."
)


async def follow_up_across_zero(seconds: int) -> None:
    # The follow-up delay is resolved on the client into the session input.
    os.environ["FOLLOW_UP_DELAY"] = str(seconds)
    cid = f"serverless-follow-up-{int(time.time())}"
    config.validate_model_credentials()
    client = await config.connect_client()
    print(f"session id (workflow id): {cid}   follow-up in {seconds}s")
    print("starting session with NO local worker and NO manual invoke...\n", flush=True)
    start = time.time()
    await signal_with_start(client, cid, FOLLOW_UP_MESSAGE, scenario="deferred_follow_up")
    handle = client.get_workflow_handle(cid)
    seen: set[str] = set()
    activities = 0
    while time.time() - start < seconds + TIMEOUT:
        elapsed = time.time() - start
        names = []
        async for event in handle.fetch_history_events():
            kind = event.WhichOneof("attributes")
            if kind == "activity_task_scheduled_event_attributes":
                names.append(event.activity_task_scheduled_event_attributes.activity_type.name)
            elif kind in ("timer_started_event_attributes", "timer_fired_event_attributes"):
                if kind not in seen:
                    seen.add(kind)
                    if kind == "timer_started_event_attributes":
                        print(f"[{elapsed:5.1f}s] follow-up armed: a durable Timer. The Worker "
                              "drains when idle; nothing runs until the Timer fires.", flush=True)
                    else:
                        print(f"[{elapsed:5.1f}s] Timer fired: Temporal starts a Worker in "
                              "AgentCore again", flush=True)
        for name in names[activities:]:
            print(f"[{elapsed:5.1f}s] activity: {name}", flush=True)
        activities = len(names)
        if "timer_fired_event_attributes" in seen and "send_customer_message" in names:
            print(f"\nPASS: the follow-up was sent by a Worker Temporal started after the drain "
                  f"({elapsed:.0f}s).")
            return
        await asyncio.sleep(3)
    print("\nFAIL: the follow-up was not sent within the timeout. Check the wiring as for the "
          "default run (serverless.sh status).")
    raise SystemExit(1)


async def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--follow-up":
        await follow_up_across_zero(int(sys.argv[2]))
        return
    message = sys.argv[1] if len(sys.argv) > 1 else (
        "Hi, I'm looking for a used SUV under 25000. What do you have?"
    )
    cid = f"serverless-verify-{int(time.time())}"

    config.validate_model_credentials()
    client = await config.connect_client()

    print(f"session id (workflow id): {cid}")
    print(f"task queue: {TASK_QUEUE_AGENT}   model: {config.model_mode()}")
    print("starting session with NO local worker and NO manual invoke...\n", flush=True)

    start = time.time()
    await signal_with_start(client, cid, message, scenario="happy")
    handle = client.get_workflow_handle(cid)

    wci_announced = False
    while time.time() - start < TIMEOUT:
        elapsed = time.time() - start
        if not wci_announced and await _wci_seen(client):
            wci_announced = True
            print(f"[{elapsed:5.1f}s] Worker Controller Instance workflow is present "
                  "(Temporal is driving the invoke)", flush=True)
        replies = await _replies(handle)
        if replies:
            print(f"\n[{elapsed:5.1f}s] REPLY (a Worker ran inside AgentCore, "
                  "started by Temporal):\n")
            print(replies[-1]["text"])
            print("\nPASS: the turn completed with no local Worker.")
            return
        await asyncio.sleep(2)

    print("\nFAIL: no reply within the timeout. Check: runtime is versioned "
          "(set_runtime_versioning.py), the version is current with the AgentCore "
          "compute provider (serverless.sh status), and the invoke role trust + "
          "External ID match.")
    raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
