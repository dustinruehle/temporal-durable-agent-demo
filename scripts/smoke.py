"""Stage 2 mock smoke: one session, one message, one reply.

Called by Preflight (against its throwaway dev server). Runs
the stage 2 workers in-process, drives one happy message through Signal-With-Start,
and asserts a reply comes back. Exits non-zero on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from temporalio.client import Client  # noqa: E402

from durable_agent.config import build_plugin  # noqa: E402
from durable_agent.session_client import signal_with_start  # noqa: E402
from durable_agent.worker import running_workers  # noqa: E402
from durable_agent.workflows import SessionWorkflow  # noqa: E402


async def main() -> int:
    target = (
        os.environ.get("PREFLIGHT_TARGET")
        or os.environ.get("TEMPORAL_ADDRESS")
        or "localhost:7233"
    )
    os.environ.setdefault("MOCK_SKILL_LATENCY_MS", "0")
    os.environ.setdefault("MOCK_MODEL_LATENCY_MS", "0")

    client = await Client.connect(target)
    plugin = build_plugin("mock")
    conversation_id = "smoke-session"

    async with running_workers(client, plugin):
        handle = await signal_with_start(
            client, conversation_id, "I'm looking to buy a car under 15k", "happy"
        )
        for _ in range(200):
            status = await handle.query(SessionWorkflow.status)
            if status["processed"] >= 1:
                break
            await asyncio.sleep(0.05)
        else:
            print("SMOKE FAIL: no reply within timeout", file=sys.stderr)
            return 1

        transcript = await handle.query(SessionWorkflow.transcript)
        await client.get_workflow_handle(conversation_id).signal(SessionWorkflow.close_session)

    replies = [e for e in transcript if e["kind"] == "agent_reply"]
    if not replies:
        print("SMOKE FAIL: no agent reply in transcript", file=sys.stderr)
        return 1
    print("SMOKE OK: one session, one message, one reply ->", replies[-1]["text"][:60])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
