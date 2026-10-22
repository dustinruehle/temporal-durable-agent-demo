"""Drain-on-idle tracking for the AgentCore serverless worker.

When Temporal (or a manual invoke) starts the AgentCore runtime as a Serverless
Worker, the worker should poll only while there is work, then drain so AgentCore's
idle timer can reap the microVM. This interceptor counts in-flight Activities across
every Worker it is attached to and reports when nothing has run for a debounce
window. Because the Strands plugin runs model calls as Activities and the skills are
Activities, the tracker sees the whole agent turn, so the worker is not torn down
mid-thought.
"""

from __future__ import annotations

import asyncio

from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)


class ActivityTracker(Interceptor):
    """Tracks in-flight Activities and blocks until the debounce window is quiet."""

    def __init__(self) -> None:
        self.inflight = 0
        self.changed = asyncio.Event()

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _TrackedActivity(next, self)

    async def wait_until_idle(self, debounce: float) -> None:
        """Return once no Activity has started or finished for ``debounce`` seconds."""
        while True:
            self.changed.clear()
            try:
                # Wake the moment an Activity starts or finishes; a timeout instead
                # means nothing has happened for the whole window.
                await asyncio.wait_for(self.changed.wait(), timeout=debounce)
            except asyncio.TimeoutError:
                if self.inflight == 0:
                    return


class _TrackedActivity(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, tracker: ActivityTracker) -> None:
        super().__init__(next)
        self._tracker = tracker

    async def execute_activity(self, input: ExecuteActivityInput):
        self._tracker.inflight += 1
        self._tracker.changed.set()
        try:
            return await self.next.execute_activity(input)
        finally:
            self._tracker.inflight -= 1
            self._tracker.changed.set()
