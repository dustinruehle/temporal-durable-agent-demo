"""Run any beat headless and assert its outcome. Exits non-zero on failure.

  python scripts/beat.py <n>        run beat n (1..9)

Beats 3 to 8 drive the running local stack (dev server, worker, gateway on the
default ports, as started by scripts/run.sh). Beats 1 and 2 own the stage 0 and
stage 1 harness process: they spawn it, drive a message, wait for the skill to
start, kill it, and assert. This never asks a human to hand-time a Ctrl-C; the
dashboard path is where a human does the kill live.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


from domain.config import TASK_QUEUE_MODEL  # noqa: E402
from durable_agent.config import connect_client, model_mode  # noqa: E402
from durable_agent.session_client import signal_with_start  # noqa: E402
from durable_agent.workflows import SessionWorkflow  # noqa: E402

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8090")
# --kill (beats 5 and 9): SIGKILL the running session Worker mid-session, then start a
# fresh one, so the beat proves the session survives a crash, not just an idle gap.
KILL = "--kill" in sys.argv[2:]
# --kill: pause this long after the turn lands before the kill, so the dashboard's 1 s
# poll caches the reply (its transcript comes from a Query, which needs a Worker).
_DASHBOARD_SETTLE = 2.5
FOLLOW_UP_MESSAGE = (
    "I like the cars under 15k but I need some time to think it over. "
    "Please schedule a follow-up and check back with me later."
)


def _ok(msg: str) -> None:
    print(f"BEAT OK: {msg}")


def _fail(msg: str) -> None:
    print(f"BEAT FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _conv(prefix: str) -> str:
    conv = f"{prefix}-{int(time.time())}"
    # Printed so a presenter can paste it into the dashboard's conversation id field
    # (or open http://127.0.0.1:8080/?conversation=<id>) and watch the beat live.
    print(f"  conversation id: {conv}", flush=True)
    return conv


async def _wait(handle, pred, timeout=30.0, poll=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await pred(handle):
            return True
        await asyncio.sleep(poll)
    return False


async def _processed(handle, n) -> bool:
    s = await handle.query(SessionWorkflow.status)
    return s["processed"] >= n


def _presenter_pause(conv: str) -> None:
    """--kill from a terminal: wait until the dashboard shows this conversation, so its
    transcript is on screen (and cached) before the Worker dies. Headless runs, with no
    terminal on stdin, go straight on."""
    if KILL and sys.stdin.isatty():
        input(f"  open http://127.0.0.1:8080/?conversation={conv} then press Enter to start ")


# --- --kill: crash and replace the session Worker ------------------------------


def _cwd(pid: int) -> str:
    out = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True
    ).stdout
    return next((line[1:] for line in out.splitlines() if line.startswith("n")), "")


def _session_worker_pids() -> list[int]:
    """Session Workers of THIS checkout only: started from this repo (run.sh cds here)
    or by absolute path into it (the replacement this script starts). Another copy of
    the demo on the same machine runs the same relative command, so the command line
    alone is not enough to tell them apart."""
    out = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or "--replica-only" in parts:
            continue
        # The process itself must be uv or python running worker.py, not a shell or a
        # grep whose command line merely mentions it.
        exe = Path(parts[1]).name.lower()
        if not (exe == "uv" or exe.startswith("python")) or not any(
            a.endswith("scripts/worker.py") for a in parts[2:]
        ):
            continue
        pid = int(parts[0])
        if str(REPO / "scripts" / "worker.py") in line or _cwd(pid) == str(REPO):
            pids.append(pid)
    return pids


def _kill_session_worker() -> None:
    pids = _session_worker_pids()
    if not pids:
        _fail("--kill: no session Worker running (start the stack first)")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    if _session_worker_pids():
        _fail("--kill: the session Worker is still alive")
    print(f"  killed the session Worker (SIGKILL, pids {pids}); the session has no Worker now")


def _start_session_worker() -> None:
    """Start a replacement Worker with this shell's switches. It keeps running after the
    beat, in place of the one killed, logging to run/worker-restarted.log."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    # run.sh points outbound at the gateway stub; match it so the follow-up shows there.
    env.setdefault("OUTBOUND_URL", f"{GATEWAY}/outbound")
    (REPO / "run").mkdir(exist_ok=True)
    log = open(REPO / "run" / "worker-restarted.log", "a")
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "worker.py")],
        env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    print(f"  started a fresh session Worker (pid {proc.pid}); stop it with: kill {proc.pid}")


# --- beats 1 and 2: harness process lifecycle --------------------------------


def _spawn_harness(module: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env["HARNESS_PORT"] = str(port)
    env.setdefault("MOCK_SKILL_LATENCY_MS", "4000")
    proc = subprocess.Popen(
        [sys.executable, "-m", module], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            return proc
        except httpx.HTTPError:
            time.sleep(0.25)
    proc.kill()
    _fail(f"harness {module} did not come up on {port}")
    raise AssertionError


def _feed(port: int) -> dict:
    return httpx.get(f"http://127.0.0.1:{port}/feed", timeout=2.0).json()


def _skill_started(port: int) -> bool:
    try:
        feed = _feed(port)
    except httpx.HTTPError:
        return False
    return any(e["kind"] == "tool_call" and e["status"] == "pending" for e in feed["transcript"])


def beat1() -> None:
    """Baseline and its failure: kill mid tool call, the conversation restarts empty."""
    port = 8101
    proc = _spawn_harness("harness_plain.app", port)
    try:
        httpx.post(
            f"http://127.0.0.1:{port}/message",
            json={"session_id": "s", "text": "what is my financing status?", "scenario": "handoff"},
            timeout=5.0,
        )
        # Wait for the skill to start, then kill mid call.
        for _ in range(80):
            if _skill_started(port):
                break
            time.sleep(0.1)
        else:
            _fail("beat 1: skill never started")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        # The feed is gone with the process.
        try:
            _feed(port)
            _fail("beat 1: feed still answered after kill")
        except httpx.HTTPError:
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
    # Restart: a fresh process has no memory of the prior session.
    proc2 = _spawn_harness("harness_plain.app", port)
    try:
        feed = _feed(port)
        if feed["transcript"]:
            _fail("beat 1: restarted harness has leftover transcript")
    finally:
        proc2.kill()
    _ok("beat 1: stage 0 lost the in-flight work and the session on kill")


async def _wait_activity_scheduled(activity_id: str, timeout: float = 20.0) -> bool:
    """Poll the server until the Standalone Activity is durably scheduled.

    The harness marks the tool call pending in its in-memory feed before it has
    connected its client and issued the schedule RPC, so killing on that marker
    alone races the schedule. Waiting until the server can describe the activity
    guarantees the durable record exists before the kill, which is the whole point
    of the beat.
    """
    from temporalio.service import RPCError, RPCStatusCode

    client = await connect_client()
    handle = client.get_activity_handle(activity_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            await handle.describe()
            return True
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        await asyncio.sleep(0.1)
    return False


def beat2() -> None:
    """One skill as a Standalone Activity: kill mid call, the Activity still completes."""
    from domain.config import STAGE1_ACTIVITY_ID

    port = 8102
    proc = _spawn_harness("harness_wrapped.app", port)
    try:
        httpx.post(
            f"http://127.0.0.1:{port}/message",
            json={"session_id": "s", "text": "what is my financing status?", "scenario": "handoff"},
            timeout=5.0,
        )
        for _ in range(80):
            if _skill_started(port):
                break
            time.sleep(0.1)
        else:
            _fail("beat 2: skill never started")
        # Kill only once the activity is durably on the server, so the beat proves
        # the Activity outlives the harness rather than racing the schedule RPC.
        if not asyncio.run(_wait_activity_scheduled(STAGE1_ACTIVITY_ID)):
            _fail("beat 2: Standalone Activity was never scheduled on the server")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()

    async def check() -> None:
        client = await connect_client()
        handle = client.get_activity_handle(STAGE1_ACTIVITY_ID)
        result = await handle.result()
        if "status" not in result:
            _fail("beat 2: Standalone Activity did not return a financing status")

    asyncio.run(check())
    _ok("beat 2: the Standalone Activity survived the harness kill and completed")


# --- beats 3 to 8: drive the running stack -----------------------------------


async def beat3() -> None:
    async with httpx.AsyncClient() as http:
        conv = _conv("beat3")
        r = await http.post(
            f"{GATEWAY}/inbound", json={"conversation": conv, "text": "buying under 15k"}
        )
        r.raise_for_status()
        if r.json()["workflow_id"] != conv:
            _fail("beat 3: workflow id is not the conversation id")
        client = await connect_client()
        handle = client.get_workflow_handle(conv)
        if not await _wait(handle, lambda h: _processed(h, 1)):
            _fail("beat 3: message not processed")
    _ok("beat 3: inbound curl started the session by Signal-With-Start")


async def beat4() -> None:
    client = await connect_client()
    conv = _conv("beat4")
    handle = await signal_with_start(client, conv, "buying under 15k", "happy")
    if not await _wait(handle, lambda h: _processed(h, 1)):
        _fail("beat 4: first message not processed")
    desc = await handle.describe()
    if desc.status is None or desc.status.name != "RUNNING":
        _fail("beat 4: session did not stay running while parked")
    h2 = await signal_with_start(client, conv, "any SUVs?", "happy")
    if h2.first_execution_run_id != handle.first_execution_run_id:
        _fail("beat 4: resumed a different execution")
    _ok("beat 4: the session parked and resumed in the same execution")


async def beat5() -> None:
    client = await connect_client()
    conv = _conv("beat5")
    _presenter_pause(conv)
    os.environ["FOLLOW_UP_DELAY"] = os.environ.get("FOLLOW_UP_DELAY", "3")
    # Explicit wording: a real model given only "call me later" sometimes asks a
    # clarifying question instead of calling schedule_follow_up.
    handle = await signal_with_start(client, conv, FOLLOW_UP_MESSAGE, "deferred_follow_up")
    if not await _wait(handle, lambda h: _processed(h, 1)):
        _fail("beat 5: initial message not processed")

    async def armed(h) -> bool:
        return (await h.query(SessionWorkflow.status))["follow_up_armed"]

    if not await _wait(handle, armed, timeout=10):
        _fail("beat 5: follow-up was not armed")
    if KILL:
        # Crash the Worker while the follow-up is pending. A short FOLLOW_UP_DELAY means
        # the Timer falls due during the outage; the fresh Worker sends it on restart.
        status = await handle.query(SessionWorkflow.status)
        if status["follow_up_deadline_epoch"] - time.time() > 6:
            await asyncio.sleep(_DASHBOARD_SETTLE)
        _kill_session_worker()
        due_in = status["follow_up_deadline_epoch"] - time.time()
        await asyncio.sleep(max(0.0, min(due_in, 30.0)) + 3)
        print(f"  while no Worker runs: {(await handle.describe()).status.name}")
        _start_session_worker()
        if due_in > 30:
            await handle.signal(SessionWorkflow.fast_forward_follow_up)
    else:
        # Fast-forward the wake-up so the follow-up fires now.
        await handle.signal(SessionWorkflow.fast_forward_follow_up)

    async def outbound(h) -> bool:
        t = await h.query(SessionWorkflow.transcript)
        return any(e["kind"] == "outbound" for e in t)

    if not await _wait(handle, outbound, timeout=60 if KILL else 20):
        _fail("beat 5: outbound follow-up never posted")
    if KILL:
        _ok("beat 5 (kill): the follow-up was due while no Worker ran; a fresh Worker sent it")
    else:
        _ok("beat 5: the durable follow-up fired and posted an outbound message")


async def beat6() -> None:
    client = await connect_client()
    conv = _conv("beat6")
    # A concrete application id makes a real model reach for check_financing_status
    # immediately instead of asking a clarifying question. The mock ignores the id
    # and follows the scenario script.
    handle = await signal_with_start(
        client,
        conv,
        "Please check the status of my financing application APP-7742. I want to buy a car.",
        "handoff",
    )

    async def parked(h) -> bool:
        return (await h.query(SessionWorkflow.pending_handoff)) is not None

    async def replied(h) -> bool:
        t = await h.query(SessionWorkflow.transcript)
        return any(e["kind"] == "agent_reply" for e in t)

    live = model_mode() != "mock"
    # The mock scripts financing-fail, a different approach, then the handoff in one
    # turn, so it parks straight away. A real model reports the failed financing
    # check and yields the turn, and it may legitimately resolve the need by offering
    # an in-budget vehicle it found; one direct nudge that the customer wants a human
    # is what reliably drives it to the back-office handoff. Poll for a park first; if
    # the model replied instead, nudge (once for the mock, up to twice for a live
    # model, since it can take an extra turn to exhaust the alternatives).
    if not await _wait(handle, parked, timeout=30):
        nudges = [
            "None of those work for me. Please have someone from your back office "
            "team contact me directly to sort out my financing.",
            "I do not want another car option. I need a human from your team to take "
            "over and resolve this. Please hand me off.",
        ]
        for text in nudges if live else nudges[:1]:
            if not await replied(handle):
                break
            await handle.signal(SessionWorkflow.deliver_message, text)
            if await _wait(handle, parked, timeout=120):
                break
        if not await parked(handle):
            _fail("beat 6: handoff never parked")
    await handle.signal(SessionWorkflow.respond_handoff, args=["approved", "back office"])
    if not await _wait(handle, lambda h: _processed(h, 1), timeout=20):
        _fail("beat 6: session did not resume after handoff")
    t = await handle.query(SessionWorkflow.transcript)
    calls = [e["tool"] for e in t if e["kind"] == "tool_call"]
    fin = [e for e in t if e["kind"] == "tool_result" and e["tool"] == "check_financing_status"]
    financing_failed = any(e.get("status") == "failed" for e in fin)
    if live:
        # A real model improvises the exact branch order (it may or may not try
        # inventory before escalating), so assert the durable properties instead of a
        # scripted sequence: the financing skill was attempted and durably failed,
        # and the run reached and resumed the human handoff.
        if not financing_failed:
            _fail(f"beat 6: financing skill never durably failed: {calls}")
        if "hand_off_to_back_office" not in calls:
            _fail(f"beat 6: never reached the human handoff: {calls}")
        _ok("beat 6 (live): durable skill failure, escalation, then a human handoff")
    else:
        if calls != ["check_financing_status", "check_inventory", "hand_off_to_back_office"]:
            _fail(f"beat 6: branches out of order: {calls}")
        _ok("beat 6: mechanical retry, a different approach, then a human handoff")


async def _model_call_starts(handle) -> list[float]:
    """Start times of this session's model calls, read from its Event History."""
    model_sched: set[int] = set()
    starts: list[float] = []
    async for e in handle.fetch_history_events():
        if e.HasField("activity_task_scheduled_event_attributes"):
            if e.activity_task_scheduled_event_attributes.task_queue.name == TASK_QUEUE_MODEL:
                model_sched.add(e.event_id)
        elif e.HasField("activity_task_started_event_attributes"):
            if e.activity_task_started_event_attributes.scheduled_event_id in model_sched:
                starts.append(e.event_time.ToDatetime().timestamp())
    return starts


async def beat7() -> None:
    # Assumes scripts/run.sh --workers 2 is running, so two model-calls replicas
    # poll the queue. Drive several sessions at once and measure how fast the
    # Service hands out model calls across both replicas.
    rps = float(os.environ.get("MODEL_QUEUE_RPS", "6"))
    client = await connect_client()
    convs = [_conv(f"beat7-{i}") for i in range(12)]
    handles = await asyncio.gather(
        *[signal_with_start(client, c, "buying under 15k", "happy") for c in convs]
    )
    for h in handles:
        if not await _wait(h, lambda x: _processed(x, 1), timeout=60):
            _fail("beat 7: a session did not complete")
    per_session = [await _model_call_starts(h) for h in handles]
    starts = sorted(t for session in per_session for t in session)
    if len(starts) < 2:
        _fail("beat 7: no model calls found in Event History")
    # Every session asks the model at once, so a backlog builds on the model queue.
    # The Service lets a small burst through in the first second, then paces the
    # queue at the cap across both replicas. Measure while the backlog lasts: until
    # the last session's first model call starts, calls are always waiting.
    counts = _per_second(starts)
    print(f"  model call starts per second: {counts}")
    backlog_end = max(min(session) for session in per_session)
    window = [t for t in starts if starts[0] + 1.0 <= t <= backlog_end]
    span = window[-1] - window[0] if len(window) > 1 else 0.0
    if span <= 0:
        _fail("beat 7: no backlog formed; lower MODEL_QUEUE_RPS so the cap binds")
    paced = (len(window) - 1) / span
    if paced > rps * 1.25:
        _fail(f"beat 7: model calls ran at {paced:.1f} rps under backlog, over the {rps:g} rps cap")
    _ok(
        f"beat 7: {len(starts)} model calls across two replicas: {counts[0]} in the first "
        f"second (the burst allowance), then {paced:.1f} rps while calls queued, "
        f"against a {rps:g} rps cap"
    )


def _per_second(starts: list[float]) -> list[int]:
    first = starts[0]
    counts = [0] * (int(starts[-1] - first) + 1)
    for t in starts:
        counts[int(t - first)] += 1
    return counts


async def beat8() -> None:
    client = await connect_client()
    conv = _conv("beat8")
    handle = await signal_with_start(client, conv, "buying under 15k", "happy")
    if not await _wait(handle, lambda h: _processed(h, 1)):
        _fail("beat 8: first message not processed")
    latest = client.get_workflow_handle(conv)
    run_a = (await latest.describe()).run_id
    await latest.signal(SessionWorkflow.force_continue_as_new)

    async def rolled(h) -> bool:
        return (await h.describe()).run_id != run_a

    if not await _wait(latest, rolled, timeout=20):
        _fail("beat 8: no Continue-As-New")
    status = await latest.query(SessionWorkflow.status)
    if status["carried_messages"] <= 0:
        _fail("beat 8: conversation was not carried across the boundary")
    _ok("beat 8: rolled over to a fresh run carrying the conversation")


async def beat9() -> None:
    """Inactivity and rehydration: a 15-minute idle reap wipes a naive session, but
    the durable session survives the same gap and resumes with no rehydration code.

    The idle reap is the AgentCore microVM (or any process) shutting down after a
    quiet period. We mimic it instantly by killing the process rather than waiting
    the real fifteen minutes.
    """
    # --- naive lane (stage 0): the process is the state, so an idle reap loses it ---
    port = 8109
    proc = _spawn_harness("harness_plain.app", port)
    try:
        httpx.post(
            f"http://127.0.0.1:{port}/message",
            json={"session_id": "idle", "text": "I am looking to buy under 15k", "scenario": "happy"},
            timeout=5.0,
        )
        for _ in range(120):
            if any(e["kind"] == "agent_reply" for e in _feed(port)["transcript"]):
                break
            time.sleep(0.1)
        else:
            _fail("beat 9: naive harness never replied")
        # The 15-minute idle reap: the microVM (process) shuts down.
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    reaped = _spawn_harness("harness_plain.app", port)
    try:
        if _feed(port)["transcript"]:
            _fail("beat 9: naive harness kept state across the idle reap")
        # A follow-up on the fresh process has no memory of the earlier turn; without
        # rehydration-from-store code, the pre-reap turn is gone.
        httpx.post(
            f"http://127.0.0.1:{port}/message",
            json={"session_id": "idle", "text": "book the first one", "scenario": "happy"},
            timeout=5.0,
        )
        for _ in range(120):
            if any(e["kind"] == "agent_reply" for e in _feed(port)["transcript"]):
                break
            time.sleep(0.1)
        custs = [e["text"] for e in _feed(port)["transcript"] if e["kind"] == "customer_message"]
        if any("15k" in c for c in custs):
            _fail(f"beat 9: naive harness recovered the pre-reap turn unexpectedly: {custs}")
    finally:
        reaped.kill()

    # --- durable lane (stage 2): the state lives in Temporal, not the process ---
    client = await connect_client()
    conv = _conv("beat9")
    _presenter_pause(conv)
    await signal_with_start(client, conv, "I am looking to buy under 15k", "happy")
    handle = client.get_workflow_handle(conv)
    if not await _wait(handle, lambda h: _processed(h, 1)):
        _fail("beat 9: durable session did not process the first message")
    desc = await handle.describe()
    if desc.status is None or desc.status.name != "RUNNING":
        _fail("beat 9: durable session did not stay RUNNING while idle")
    first_run = desc.run_id
    if KILL:
        # The reap for real: the Worker process dies, and a fresh one picks the
        # session up from Temporal when the next message arrives.
        await asyncio.sleep(_DASHBOARD_SETTLE)
        _kill_session_worker()
        await asyncio.sleep(2)
        print(f"  while no Worker runs: {(await handle.describe()).status.name}")
        _start_session_worker()
    # A follow-up after the idle gap resumes the SAME execution with the earlier turn
    # intact, so nothing had to be reloaded.
    await handle.signal(SessionWorkflow.deliver_message, "book the first one")
    if not await _wait(handle, lambda h: _processed(h, 2), timeout=60 if KILL else 30):
        _fail("beat 9: durable session did not resume after the idle gap")
    t = await handle.query(SessionWorkflow.transcript)
    custs = [e["text"] for e in t if e["kind"] == "customer_message"]
    if not any("15k" in c for c in custs):
        _fail(f"beat 9: durable session lost the pre-gap turn: {custs}")
    if (await handle.describe()).run_id != first_run:
        _fail("beat 9: durable session did not resume the same execution")
    await handle.signal(SessionWorkflow.close_session)
    if KILL:
        _ok("beat 9 (kill): the naive session was lost; the durable session resumed on a fresh Worker")
    else:
        _ok("beat 9: the idle reap wiped the naive session; the durable session resumed with no rehydration")


_ASYNC = {3: beat3, 4: beat4, 5: beat5, 6: beat6, 7: beat7, 8: beat8, 9: beat9}
_SYNC = {1: beat1, 2: beat2}


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit() or set(sys.argv[2:]) - {"--kill"}:
        _fail("usage: python scripts/beat.py <n> [--kill]  (n in 1..9; --kill for 5 and 9)")
    if KILL and sys.argv[1] not in ("5", "9"):
        _fail("--kill applies to beats 5 and 9")
    n = int(sys.argv[1])
    if n in _SYNC:
        _SYNC[n]()
    elif n in _ASYNC:
        asyncio.run(_ASYNC[n]())
    else:
        _fail(f"no such beat: {n}")


if __name__ == "__main__":
    main()
