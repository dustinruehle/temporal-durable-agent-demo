"""Preflight probe: prove Standalone Activities work end to end.

Section 5 requires Preflight to actively prove that Standalone Activities run,
not merely to check a version number. The activity.enableStandalone dynamic
config flag is disabled by default, and stage 1 depends on it.

This script connects to a throwaway dev server (started by preflight.sh with the
flag enabled), runs a Worker on a throwaway task queue, executes a trivial
Standalone Activity from the client with client.execute_activity, and asserts the
result. It exits 0 on success and non-zero with a clear message on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

PROBE_QUEUE = "preflight-standalone-probe"
PROBE_ID = "preflight-standalone-probe-activity"


@activity.defn
async def probe_echo(value: str) -> str:
    """Trivial activity used only to prove Standalone Activities execute."""
    return f"standalone:{value}"


async def main() -> int:
    target = os.environ.get("PREFLIGHT_TARGET", "localhost:7233")
    try:
        client = await Client.connect(target)
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE FAIL: could not connect to {target}: {exc}", file=sys.stderr)
        return 1

    worker = Worker(client, task_queue=PROBE_QUEUE, activities=[probe_echo])
    async with worker:
        try:
            result = await client.execute_activity(
                probe_echo,
                args=["ok"],
                id=PROBE_ID,
                task_queue=PROBE_QUEUE,
                start_to_close_timeout=timedelta(seconds=10),
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            print(f"PROBE FAIL: execute_activity raised: {msg}", file=sys.stderr)
            if "enableStandalone" in msg or "standalone" in msg.lower():
                print(
                    "REMEDIATION: start the dev server with "
                    "--dynamic-config-value activity.enableStandalone=true",
                    file=sys.stderr,
                )
            return 1

    if result != "standalone:ok":
        print(f"PROBE FAIL: unexpected result {result!r}", file=sys.stderr)
        return 1

    print("PROBE OK: Standalone Activity executed end to end ->", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
