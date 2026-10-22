"""Run the stage 2 workers.

Two task queues, one process by default. The agent-session worker hosts the
session Workflow and the skill Activities. The model-calls worker hosts the model
Activity. A second model-calls replica can be launched for beat 7; the throughput
cap is enforced task-queue-wide by the Temporal Service, so the replicas need no
coordination between them.

Startable on its own so scripts/worker.py can launch extra replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
from collections.abc import AsyncIterator

from temporalio.client import Client
from temporalio.worker import Worker

from domain import config as domain_config
from domain import skills
from domain.config import TASK_QUEUE_AGENT, TASK_QUEUE_MODEL
from durable_agent import config
from durable_agent.workflows import SessionWorkflow


@contextlib.asynccontextmanager
async def running_workers(
    client: Client,
    plugin,
    model_replicas: int = 1,
    include_agent_worker: bool = True,
    model_queue_rps: float | None = None,
    interceptors: list | None = None,
    deployment_config=None,
) -> AsyncIterator[None]:
    """Start the workers as an async context; stop them cleanly on exit.

    include_agent_worker is False for an extra replica process that should only
    add model-calls capacity (beat 7).

    model_queue_rps applies a task-queue-wide activity rate cap on the model-calls
    queue. This is the server-enforced limit, so it holds across every replica
    polling the queue with no coordination between them (beat 7). The per-Worker
    knob would let two replicas run at twice the cap, which would make the beat a
    counterexample to the thing it demonstrates, so it is deliberately not used.
    Every model-calls Worker reports the same value.

    interceptors are Worker interceptors applied to every Worker started here. The
    AgentCore serverless runtime passes a drain-on-idle tracker so it can tell when
    the agent's turn is finished and the microVM can be reaped.

    deployment_config is an optional WorkerDeploymentConfig for Worker Versioning.
    Serverless Workers (the WCI invoking AgentCore on demand) require it; the local
    demo leaves it None so the beats and offline suite run unversioned.
    """
    model_kwargs: dict = {}
    if model_queue_rps is not None:
        model_kwargs["max_task_queue_activities_per_second"] = model_queue_rps

    common: dict = {}
    if interceptors is not None:
        common["interceptors"] = interceptors
    if deployment_config is not None:
        common["deployment_config"] = deployment_config

    async with contextlib.AsyncExitStack() as stack:
        if include_agent_worker:
            await stack.enter_async_context(
                Worker(
                    client,
                    task_queue=TASK_QUEUE_AGENT,
                    workflows=[SessionWorkflow],
                    activities=skills.ALL_SKILLS,
                    plugins=[plugin],
                    **common,
                )
            )
        for _ in range(model_replicas):
            await stack.enter_async_context(
                Worker(
                    client,
                    task_queue=TASK_QUEUE_MODEL,
                    plugins=[plugin],
                    **model_kwargs,
                    **common,
                )
            )
        yield


async def main(model_replicas: int = 1, include_agent_worker: bool = True) -> None:
    config.guard_runtime_host()
    config.validate_model_credentials()
    client = await config.connect_client()
    plugin = config.build_plugin()
    rps = domain_config.model_queue_rps()
    print(
        f"worker up: target={config.temporal_target()} model={config.model_mode()} "
        f"skills={config.skills_mode()} "
        f"queues={f'{TASK_QUEUE_AGENT},{TASK_QUEUE_MODEL}' if include_agent_worker else TASK_QUEUE_MODEL} "
        f"model_replicas={model_replicas} model_queue_rps={rps}",
        flush=True,
    )
    stop = _install_stop_handlers()
    async with running_workers(
        client,
        plugin,
        model_replicas=model_replicas,
        include_agent_worker=include_agent_worker,
        model_queue_rps=rps,
    ):
        # Run until Ctrl-C (SIGINT) or kill (SIGTERM).
        await stop.wait()
        print("\nworker stopping", flush=True)
        _exit_if_shutdown_hangs()


# Seconds the Workers get to shut down after a stop signal before the process
# exits anyway. The demo's Activities are short and idempotent, so a cut-off
# Activity is retried on the next Worker.
_SHUTDOWN_GRACE_SECONDS = 5.0


def _install_stop_handlers() -> asyncio.Event:
    """Return an Event set by SIGINT or SIGTERM. A second signal exits at once.

    The handlers are installed explicitly because a process started in the
    background by a shell script (run.sh) inherits SIGINT as ignored, so the
    default Ctrl-C handling would never reach it.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_signal(signum: int) -> None:
        if stop.is_set():
            os._exit(128 + signum)
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, on_signal, signum)
    return stop


def _exit_if_shutdown_hangs() -> None:
    """Exit the process if Worker shutdown takes longer than the grace period."""
    timer = threading.Timer(_SHUTDOWN_GRACE_SECONDS, os._exit, args=(0,))
    timer.daemon = True
    timer.start()


if __name__ == "__main__":
    asyncio.run(main())
