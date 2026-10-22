"""The session Workflow: one long-lived agent that follows a customer journey.

An inbound message starts or signals this Workflow, keyed by the conversation id
(the Workflow Id). The Workflow holds conversation state, runs the agent loop, and
records a transcript that the dashboard queries. The model runs as an Activity on
the model-calls queue; the skills run as Activities on the agent-session queue.

The Workflow reads no environment and no files. Everything it needs arrives in
SessionInput, so replay is deterministic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from strands import tool
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent
from strands.types.interrupt import InterruptResponseContent
from temporalio import workflow
from temporalio.common import SearchAttributeKey
from temporalio.contrib.strands import TemporalAgent
from temporalio.contrib.strands.workflow import activity_as_tool
from temporalio.exceptions import ActivityError

from domain import skills
from domain.config import TASK_QUEUE_AGENT, TASK_QUEUE_MODEL
from domain.markers import FOLLOW_UP_DUE_MARKER
from domain.transcript import (
    AGENT_REPLY,
    CUSTOMER_MESSAGE,
    OUTBOUND,
    SYSTEM,
    TOOL_CALL,
    TOOL_RESULT,
    TranscriptEvent,
)
from durable_agent.session_types import SessionInput

# The model call can take real seconds with a live provider, so give it headroom.
_MODEL_TIMEOUT = timedelta(seconds=120)

# The armed follow-up deadline, published so planned wake-ups across every session are
# one list filter: NextFollowUpAt is not null, sorted by time. Register it once per
# namespace as a Datetime Search Attribute.
NEXT_FOLLOW_UP_AT = SearchAttributeKey.for_datetime("NextFollowUpAt")


class ApprovalHook(HookProvider):
    """Beat 6: gate the handoff tool behind a back-office decision.

    Before hand_off_to_back_office runs, the hook raises an interrupt. The agent
    returns with stop_reason interrupt and the Workflow parks on a Signal at zero
    Worker cost until the back office responds, then resumes the agent with the
    decision. This is the Approval pattern applied to an agent tool.
    """

    def __init__(self, wf: SessionWorkflow) -> None:
        self._wf = wf

    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate)

    def _gate(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") != "hand_off_to_back_office":
            return
        reason = ""
        if isinstance(event.tool_use.get("input"), dict):
            reason = event.tool_use["input"].get("reason", "")
        decision = event.interrupt("handoff", reason=reason)
        # The back office declined to resolve it: cancel the tool with the note.
        if isinstance(decision, str) and decision.lower().startswith("deny"):
            event.cancel_tool = decision


class TranscriptHook(HookProvider):
    """Capture tool calls and results into the Workflow transcript in real time.

    BeforeToolCallEvent marks a skill as started, which is also the point beat.py
    waits for before a kill. AfterToolCallEvent records the outcome, and an
    outbound send is surfaced as its own transcript line so beat 5 has a payoff.
    """

    def __init__(self, wf: SessionWorkflow) -> None:
        self._wf = wf

    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before)
        registry.add_callback(AfterToolCallEvent, self._after)

    def _before(self, event: BeforeToolCallEvent) -> None:
        # A gated tool interrupts, so BeforeToolCallEvent fires again on resume for
        # the same tool use. Emit the call once per tool use id.
        tool_use_id = event.tool_use.get("toolUseId", "")
        if tool_use_id in self._wf._seen_tool_uses:
            return
        self._wf._seen_tool_uses.add(tool_use_id)
        name = event.tool_use.get("name", "")
        self._wf._emit(TOOL_CALL, f"calling {name}", tool=name, status="pending")

    def _after(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use.get("name", "")
        failed = event.exception is not None or (
            isinstance(event.result, dict) and event.result.get("status") == "error"
        )
        status = "failed" if failed else "ok"
        self._wf._emit(TOOL_RESULT, f"{name} {status}", tool=name, status=status)
        if name == skills.send_customer_message.__name__ and not failed:
            text = ""
            if isinstance(event.tool_use.get("input"), dict):
                text = event.tool_use["input"].get("text", "")
            self._wf._emit(OUTBOUND, text, tool=name, status="ok")


@workflow.defn
class SessionWorkflow:
    def __init__(self) -> None:
        self._pending: list[str] = []
        self._transcript: list[TranscriptEvent] = []
        self._seq = 0
        self._processed = 0
        self._closed = False
        self._seen_tool_uses: set[str] = set()
        self._conversation_id = ""
        self._scenario = ""
        self._fail_count = 0
        # Beat 5 state: an armed follow-up deadline, held by the Workflow as an
        # absolute epoch second so a boundary delay cannot shift it (beat 8).
        self._follow_up_deadline_epoch: float | None = None
        self._follow_up_default = 0
        self._follow_up_note = ""
        self._follow_up_requested = False
        self._deadline_changed = False
        self._track_follow_up = False
        self._published_deadline: float | None = None
        # Beat 6 handoff state.
        self._pending_handoff_reason: str | None = None
        self._handoff_response: str | None = None
        # Beat 8 Continue-As-New state.
        self._force_can = False
        self._carried_message_count = 0

    # --- signals and queries --------------------------------------------------

    @workflow.signal
    def deliver_message(self, text: str) -> None:
        """Inbound customer message. The gateway calls this via Signal-With-Start."""
        self._pending.append(text)

    @workflow.signal
    def close_session(self) -> None:
        self._closed = True

    @workflow.signal
    def customer_replies_now(self, text: str) -> None:
        """Beat 5: the customer re-engages before the wake-up.

        Deliver the reply and cancel the pending follow-up, because the customer
        answering is what the follow-up was waiting for.
        """
        self._pending.append(text)
        self._cancel_follow_up()

    @workflow.signal
    def fast_forward_follow_up(self) -> None:
        """Beat 5: move the pending wake-up deadline to now so it fires at once.

        This is a real Signal that moves a real deadline. Time is not faked.
        """
        if self._follow_up_deadline_epoch is not None:
            self._follow_up_deadline_epoch = workflow.now().timestamp()
            self._deadline_changed = True

    @workflow.query
    def transcript(self) -> list[dict]:
        return [e.to_dict() for e in self._transcript]

    @workflow.query
    def status(self) -> dict:
        return {
            "conversation_id": self._conversation_id,
            "scenario": self._scenario,
            "processed": self._processed,
            "pending": len(self._pending),
            "follow_up_armed": self._follow_up_deadline_epoch is not None,
            "follow_up_deadline_epoch": self._follow_up_deadline_epoch,
            "carried_messages": self._carried_message_count,
        }

    @workflow.signal
    def respond_handoff(self, decision: str, approver: str) -> None:
        """Beat 6: the back office responds. Resumes the parked session.

        The decision, the approver, and the time land in Event History as this
        signal and the transcript line it writes, with no separate logging code.
        """
        self._handoff_response = decision
        self._emit(
            SYSTEM,
            f"back office responded: {decision} by {approver}",
            detail={
                "decision": decision,
                "approver": approver,
                "at": workflow.now().isoformat(),
            },
        )

    @workflow.signal
    def force_continue_as_new(self) -> None:
        """Beat 8: force the journey to roll over to a fresh run when next idle.

        The dashboard and tests use this to trigger Continue-As-New on demand; a
        long-running session also rolls over on its own when the history grows.
        """
        self._force_can = True
        self._deadline_changed = True

    @workflow.query
    def pending_handoff(self) -> str | None:
        return self._pending_handoff_reason

    def _cancel_follow_up(self) -> None:
        if self._follow_up_deadline_epoch is not None:
            self._follow_up_deadline_epoch = None
            self._deadline_changed = True
            self._emit(SYSTEM, "follow-up cancelled")

    # --- transcript helper ----------------------------------------------------

    def _emit(
        self,
        kind: str,
        text: str,
        tool: str | None = None,
        status: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self._seq += 1
        self._transcript.append(
            TranscriptEvent(
                kind=kind,
                text=text,
                seq=self._seq,
                tool=tool,
                status=status,
                detail=detail or {},
            )
        )

    # --- tools ----------------------------------------------------------------

    def _build_tools(self) -> list:
        wf = self

        # In-workflow tools are async so Strands runs them on the Workflow event
        # loop. A sync tool would be dispatched to a thread pool, which the
        # Workflow sandbox forbids.
        @tool
        async def schedule_follow_up(note: str = "") -> str:
            """Record intent to follow up with the customer later. Does not sleep.

            Only records the request. The Workflow arms the absolute deadline and
            holds it, so a Signal can later shorten, fast-forward, or cancel it.
            """
            wf._follow_up_requested = True
            wf._follow_up_note = note
            return "Follow-up armed."

        @tool
        async def hand_off_to_back_office(reason: str = "") -> str:
            """Escalate to the back office for a human decision."""
            return "Handed off and resolved."

        @tool
        async def check_financing_status(application_id: str = "", fail_attempts: int = 0) -> dict:
            """Look up the status of an existing financing application.

            The financing skill runs with a fixed-count Retry Policy. When the
            retries exhaust, the failure is returned to the model as a tool error
            rather than propagating as an Activity failure that would fail the
            Workflow. That plumbing choice is what lets the model try a different
            approach (beat 6, branch two). The failure count comes from the session
            config, not the model, so the beat fails deterministically with any
            model rather than depending on the mock injecting it.
            """
            opts = skills.skill_options("check_financing_status")
            try:
                return await workflow.execute_activity(
                    skills.check_financing_status,
                    args=[application_id, wf._fail_count],
                    task_queue=TASK_QUEUE_AGENT,
                    start_to_close_timeout=opts.start_to_close_timeout,
                    retry_policy=opts.retry_policy,
                )
            except ActivityError as exc:
                # Surface as a tool error the model can read, not a Workflow failure.
                raise RuntimeError(f"financing status unavailable after retries: {exc}") from exc

        @tool
        async def send_customer_message(text: str = "") -> dict:
            """Deliver an outbound message to the customer through their channel.

            The session supplies the conversation id, so the model only writes the text
            and the message always reaches the customer this session belongs to.
            """
            opts = skills.skill_options("send_customer_message")
            return await workflow.execute_activity(
                skills.send_customer_message,
                args=[text, wf._conversation_id],
                task_queue=TASK_QUEUE_AGENT,
                start_to_close_timeout=opts.start_to_close_timeout,
                retry_policy=opts.retry_policy,
            )

        def as_tool(fn):
            opts = skills.skill_options(fn.__name__)
            return activity_as_tool(
                fn,
                task_queue=TASK_QUEUE_AGENT,
                start_to_close_timeout=opts.start_to_close_timeout,
                retry_policy=opts.retry_policy,
            )

        return [
            as_tool(skills.check_inventory),
            as_tool(skills.schedule_inspection),
            check_financing_status,
            send_customer_message,
            schedule_follow_up,
            hand_off_to_back_office,
        ]

    # --- run ------------------------------------------------------------------

    @workflow.run
    async def run(self, inp: SessionInput) -> dict:
        self._conversation_id = inp.conversation_id
        self._scenario = inp.scenario
        self._fail_count = inp.fail_count
        self._follow_up_default = inp.follow_up_seconds
        self._track_follow_up = inp.track_follow_up
        # Seed state carried across a Continue-As-New boundary (beat 8). The
        # follow-up deadline is absolute, so it re-arms at the same wall-clock
        # time in the new run regardless of any delay at the boundary.
        self._follow_up_deadline_epoch = inp.follow_up_deadline_epoch
        # Prepend carried pending rather than assigning, so a message already
        # delivered by Signal-With-Start (its handler runs before run() executes)
        # is not clobbered.
        self._pending = list(inp.pending) + self._pending
        self._carried_message_count = len(inp.messages)
        if self._follow_up_deadline_epoch is not None:
            self._emit(SYSTEM, "follow-up re-armed after roll-over")

        agent = TemporalAgent(
            model=inp.model,
            task_queue=TASK_QUEUE_MODEL,
            start_to_close_timeout=_MODEL_TIMEOUT,
            system_prompt=inp.system_prompt,
            tools=self._build_tools(),
            # ApprovalHook is registered first so a gated tool interrupts before
            # the TranscriptHook records the call, otherwise the interrupt-then-
            # resume path would record the handoff tool call twice.
            hooks=[ApprovalHook(self), TranscriptHook(self)],
            messages=list(inp.messages),
        )

        while not self._closed:
            self._publish_follow_up()
            self._maybe_continue_as_new(agent, inp)
            fired = await self._await_next(agent)
            if fired == "closed":
                break
            if self._pending:
                text = self._pending.pop(0)
                await self._handle_message(agent, text)

        self._follow_up_deadline_epoch = None
        self._publish_follow_up()
        return {"processed": self._processed, "transcript": self.transcript()}

    def _maybe_continue_as_new(self, agent: TemporalAgent, inp: SessionInput) -> None:
        """Roll over to a fresh run when idle, carrying the state that matters.

        Carries the agent's messages (the conversation), the pending queue, and
        the absolute follow-up deadline so scheduled intent survives the boundary.
        Only rolls over when idle: nothing pending and no handoff parked.
        """
        should = self._force_can or workflow.info().is_continue_as_new_suggested()
        if not should or self._pending or self._pending_handoff_reason is not None:
            return
        self._force_can = False
        workflow.continue_as_new(
            SessionInput(
                system_prompt=inp.system_prompt,
                model=inp.model,
                scenario=inp.scenario,
                conversation_id=inp.conversation_id,
                follow_up_seconds=inp.follow_up_seconds,
                messages=list(agent.messages),
                follow_up_deadline_epoch=self._follow_up_deadline_epoch,
                pending=list(self._pending),
                track_follow_up=inp.track_follow_up,
            )
        )

    async def _await_next(self, agent: TemporalAgent) -> str:
        """Wait for the next thing to do: a message, close, or a follow-up firing.

        When a follow-up is armed, this is the updatable-timer sleep: it waits up
        to the remaining interval, and a Signal that moves or cancels the deadline
        wakes it to recompute. Returns "closed", "fired", or "message".
        """
        if self._follow_up_deadline_epoch is not None:
            remaining = self._follow_up_deadline_epoch - workflow.now().timestamp()
            if remaining <= 0:
                await self._fire_follow_up(agent)
                return "fired"
            self._deadline_changed = False
            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending)
                    or self._closed
                    or self._deadline_changed
                    or self._force_can,
                    timeout=timedelta(seconds=remaining),
                )
            except asyncio.TimeoutError:
                await self._fire_follow_up(agent)
                return "fired"
            if self._closed and not self._pending:
                return "closed"
            return "message" if self._pending else "idle"

        await workflow.wait_condition(
            lambda: bool(self._pending) or self._closed or self._force_can
        )
        if self._closed and not self._pending:
            return "closed"
        return "message" if self._pending else "idle"

    async def _fire_follow_up(self, agent: TemporalAgent) -> None:
        """The deadline reached: re-invoke the agent to send an outbound check-in."""
        self._follow_up_deadline_epoch = None
        self._deadline_changed = True
        self._publish_follow_up()
        self._emit(SYSTEM, "follow-up fired")
        result = await agent.invoke_async(
            f"{FOLLOW_UP_DUE_MARKER} the scheduled follow-up is due; check in with the customer"
        )
        self._emit(AGENT_REPLY, str(result))

    async def _handle_message(self, agent: TemporalAgent, text: str) -> None:
        self._emit(CUSTOMER_MESSAGE, text)
        result = await self._invoke_with_handoff(agent, text)
        self._arm_follow_up_if_requested()
        self._emit(AGENT_REPLY, str(result))
        self._processed += 1

    async def _invoke_with_handoff(self, agent: TemporalAgent, message):
        """Run the agent, parking on a Signal whenever it interrupts for a handoff."""
        result = await agent.invoke_async(message)
        while result.stop_reason == "interrupt":
            interrupts = list(result.interrupts or [])
            self._pending_handoff_reason = interrupts[0].reason if interrupts else "handoff"
            self._emit(SYSTEM, "handed off to back office", detail={"reason": self._pending_handoff_reason})
            await workflow.wait_condition(lambda: self._handoff_response is not None)
            response = self._handoff_response
            self._handoff_response = None
            self._pending_handoff_reason = None
            responses: list[InterruptResponseContent] = [
                {"interruptResponse": {"interruptId": i.id, "response": response}}
                for i in interrupts
            ]
            result = await agent.invoke_async(responses)
        return result

    def _arm_follow_up_if_requested(self) -> None:
        """Arm the absolute deadline the agent asked for, in Workflow context.

        The deadline is computed here, not in the tool, so workflow.now() runs on
        the Workflow thread. Absolute epoch seconds so a boundary delay (beat 8)
        cannot shift it.
        """
        if self._follow_up_requested and self._follow_up_deadline_epoch is None:
            self._follow_up_requested = False
            self._follow_up_deadline_epoch = workflow.now().timestamp() + self._follow_up_default
            self._deadline_changed = True
            self._emit(SYSTEM, "follow-up armed", detail={"note": self._follow_up_note})
            self._publish_follow_up()

    def _publish_follow_up(self) -> None:
        """Mirror the follow-up deadline into NextFollowUpAt when it has changed.

        Called on arm and fire, and at the top of every loop pass so a Signal that
        moves or cancels the deadline is published too. Unset means nothing planned.
        """
        if not self._track_follow_up or self._follow_up_deadline_epoch == self._published_deadline:
            return
        if self._follow_up_deadline_epoch is None:
            update = NEXT_FOLLOW_UP_AT.value_unset()
        else:
            at = datetime.fromtimestamp(self._follow_up_deadline_epoch, tz=timezone.utc)
            update = NEXT_FOLLOW_UP_AT.value_set(at)
        workflow.upsert_search_attributes([update])
        self._published_deadline = self._follow_up_deadline_epoch
