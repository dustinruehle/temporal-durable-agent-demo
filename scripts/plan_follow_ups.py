"""Seed sessions with follow-ups at staggered times, for the planned-tasks view.

Each session asks the agent to check back later, so it arms a follow-up and publishes
the deadline as NextFollowUpAt. The list filter then shows every planned wake-up
across sessions, soonest first:

  NextFollowUpAt IS NOT NULL AND ExecutionStatus = "Running" AND TaskQueue = "<agent queue>"

Needs the stack up (a session Worker), NextFollowUpAt registered on the namespace,
and FOLLOW_UP_SEARCH_ATTRIBUTE=on (run.sh turns it on locally).

Usage:
  uv run python scripts/plan_follow_ups.py [hours ...]   # default: 1 4 24 72
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from domain.config import TASK_QUEUE_AGENT  # noqa: E402
from durable_agent.config import connect_client  # noqa: E402
from durable_agent.session_client import signal_with_start  # noqa: E402
from durable_agent.workflows import SessionWorkflow  # noqa: E402
from durable_agent.workflows.session import NEXT_FOLLOW_UP_AT  # noqa: E402

MESSAGE = (
    "I like the cars under 15k but I need some time to think it over. "
    "Please schedule a follow-up and check back with me later."
)
# Scoped to this demo's session task queue, so sessions from another copy of the demo
# on the same Namespace stay out of the list.
QUERY = (
    'NextFollowUpAt IS NOT NULL AND ExecutionStatus = "Running" '
    f'AND TaskQueue = "{TASK_QUEUE_AGENT}"'
)


async def _plan(client, hours: float) -> str:
    # The follow-up delay is resolved on the client into the session input.
    os.environ["FOLLOW_UP_DELAY"] = str(int(hours * 3600))
    conv = f"planned-{int(hours)}h-{int(time.time())}"
    handle = await signal_with_start(client, conv, MESSAGE, "deferred_follow_up")
    for _ in range(120):
        at = (await handle.describe()).typed_search_attributes.get(NEXT_FOLLOW_UP_AT)
        if at is not None:
            print(f"  {conv}: follow-up at {at:%Y-%m-%d %H:%M} UTC")
            return conv
        await asyncio.sleep(0.5)
    status = await handle.query(SessionWorkflow.status)
    print(f"  {conv}: no follow-up published (armed={status['follow_up_armed']})")
    return conv


async def main() -> None:
    os.environ.setdefault("FOLLOW_UP_SEARCH_ATTRIBUTE", "on")
    hours = [float(h) for h in sys.argv[1:]] or [1, 4, 24, 72]
    client = await connect_client()
    print("planning follow-ups:")
    planned = [await _plan(client, h) for h in hours]
    # Visibility is eventually consistent, so wait until every new session is listed.
    for _ in range(20):
        rows = [wf async for wf in client.list_workflows(QUERY)]
        if set(planned) <= {wf.id for wf in rows}:
            break
        await asyncio.sleep(0.5)
    print(f"\nplanned wake-ups across sessions ({QUERY}):")
    rows.sort(key=lambda wf: wf.typed_search_attributes.get(NEXT_FOLLOW_UP_AT))
    for wf in rows:
        print(f"  {wf.typed_search_attributes.get(NEXT_FOLLOW_UP_AT):%Y-%m-%d %H:%M} UTC  {wf.id}")


if __name__ == "__main__":
    asyncio.run(main())
