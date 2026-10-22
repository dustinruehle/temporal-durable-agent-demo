"""The harness loop: a plain Strands agent with hand-rolled plumbing.

Conversation state lives in a dict keyed by session id, retries are a hand-rolled
wrapper, and a semaphore stands in for a token-per-minute limiter. There is no
scheduler. The transcript is held in memory and served over a tiny local endpoint;
when the process dies the endpoint stops answering and the conversation is gone.

The only difference between the stage 0 and stage 1 copies of this file is how
_check_financing_status runs, so a file-to-file diff is the literal cost of
adopting durability on a single step.
"""

from __future__ import annotations

import asyncio

from strands import Agent, tool

from domain import backend
from domain.config import skill_latency_ms
from domain.mock_model import MockModel
from domain.prompt import build_system_prompt
from domain.scenarios import load_scenario
from domain.transcript import AGENT_REPLY, CUSTOMER_MESSAGE, TOOL_CALL, TOOL_RESULT

# A small concurrency cap standing in for a token-per-minute limiter.
_TOKEN_LIMIT = 4
# Hand-rolled retry attempts for the local skill call.
_RETRY_ATTEMPTS = 3


class Harness:
    stage_label = "stage 0 (plain, no Temporal)"

    def __init__(self) -> None:
        # Hand-rolled conversation state, keyed by session id. Lost on death.
        self._sessions: dict[str, list] = {}
        self._transcript: list[dict] = []
        self._seq = 0
        self._tokens = asyncio.Semaphore(_TOKEN_LIMIT)

    # --- in-memory transcript feed -------------------------------------------

    def transcript(self) -> list[dict]:
        return list(self._transcript)

    def _emit(self, kind: str, text: str, tool: str | None = None, status: str | None = None) -> None:
        self._seq += 1
        self._transcript.append(
            {"kind": kind, "text": text, "seq": self._seq, "tool": tool, "status": status, "detail": {}}
        )

    async def _with_retry(self, fn, attempts: int = _RETRY_ATTEMPTS):
        """Hand-rolled retry. Stage 2 deletes this in favor of a Retry Policy."""
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return await fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
        assert last is not None
        raise last

    # --- the one skill stage 1 will execute as a Standalone Activity ----------

    async def _check_financing_status(self, application_id: str, fail_attempts: int) -> dict:
        # STAGE 0: run the skill locally with the hand-rolled retry; a kill loses the work.
        async def call() -> dict:
            await asyncio.sleep(skill_latency_ms("check_financing_status") / 1000.0)
            return backend.financing_status(application_id)

        return await self._with_retry(call)

    # --- agent loop -----------------------------------------------------------

    def _financing_tool(self):
        harness = self

        @tool
        async def check_financing_status(application_id: str = "", fail_attempts: int = 0) -> dict:
            """Look up the status of an existing financing application."""
            harness._emit(
                TOOL_CALL, "calling check_financing_status",
                tool="check_financing_status", status="pending",
            )
            try:
                result = await harness._check_financing_status(application_id, fail_attempts)
            except Exception:
                harness._emit(
                    TOOL_RESULT, "check_financing_status failed",
                    tool="check_financing_status", status="failed",
                )
                raise
            harness._emit(
                TOOL_RESULT, "check_financing_status ok",
                tool="check_financing_status", status="ok",
            )
            return result

        return check_financing_status

    async def handle_message(
        self, session_id: str, text: str, scenario: str = "handoff", fail_count: int = 0
    ) -> str:
        self._emit(CUSTOMER_MESSAGE, text)
        messages = self._sessions.setdefault(session_id, [])
        scenario_obj = load_scenario(scenario)
        system_prompt = build_system_prompt(scenario_obj, financing_fail_override=fail_count)
        agent = Agent(
            model=MockModel(max_price=scenario_obj.max_price),
            tools=[self._financing_tool()],
            system_prompt=system_prompt,
            messages=messages,
        )
        async with self._tokens:  # token-per-minute limiter around the model call
            result = await agent.invoke_async(text)
        self._sessions[session_id] = agent.messages  # persist in the state dict
        self._emit(AGENT_REPLY, str(result))
        return str(result)
