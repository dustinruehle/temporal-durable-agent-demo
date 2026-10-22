"""The tiny local HTTP feed for the stage 0 and stage 1 harnesses.

The harness holds its transcript in memory and serves it here. When the process
dies the endpoint stops answering, which is the honest rendering of stage 0: the
dashboard shows the transcript as lost, not stale. The feed is never persisted to
a file, because a file would survive the kill and quietly undercut beat 1.

Message handling is fired in the background so a caller can post a message and
then kill the process mid tool call, which is what beats 1 and 2 require.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from fastapi import FastAPI
from pydantic import BaseModel


class HarnessLike(Protocol):
    stage_label: str

    def transcript(self) -> list[dict]: ...

    async def handle_message(
        self, session_id: str, text: str, scenario: str, fail_count: int
    ) -> str: ...


class MessageIn(BaseModel):
    session_id: str = "demo"
    text: str
    scenario: str = "handoff"
    fail_count: int = 0


def make_app(harness: HarnessLike) -> FastAPI:
    app = FastAPI(title=f"harness feed: {harness.stage_label}")
    tasks: set[asyncio.Task] = set()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "stage": harness.stage_label}

    @app.get("/feed")
    def feed() -> dict[str, Any]:
        # alive is always true while this process answers; when it dies the whole
        # endpoint stops responding, which is the signal the dashboard renders.
        return {"stage": harness.stage_label, "alive": True, "transcript": harness.transcript()}

    @app.post("/message")
    async def message(body: MessageIn) -> dict[str, Any]:
        # Fire in the background and return at once, so a caller can kill the
        # process while the tool call is still in flight.
        task = asyncio.create_task(
            harness.handle_message(body.session_id, body.text, body.scenario, body.fail_count)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"accepted": True}

    return app
