"""The same Activity definition runs both ways with no code change.

check_financing_status executes as a Standalone Activity from the client (stage 1)
and as a Workflow Activity inside a Workflow (stage 2), from one unchanged
definition, and returns the same result both ways.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from domain import backend, skills


@workflow.defn
class _FinancingWorkflow:
    @workflow.run
    async def run(self, application_id: str) -> dict:
        return await workflow.execute_activity(
            skills.check_financing_status,
            args=[application_id, 0],
            start_to_close_timeout=timedelta(seconds=30),
        )


async def test_same_activity_runs_standalone_and_in_workflow(client: Client):
    app_id = "APP-DUAL"
    expected = backend.financing_status(app_id)
    task_queue = f"dual-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[_FinancingWorkflow],
        activities=skills.ALL_SKILLS,
    ):
        # As a Standalone Activity from the client.
        standalone = await client.execute_activity(
            skills.check_financing_status,
            args=[app_id, 0],
            id=f"dual-standalone-{uuid4()}",
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=30),
        )
        # As a Workflow Activity inside a Workflow.
        in_workflow = await client.execute_workflow(
            _FinancingWorkflow.run,
            app_id,
            id=f"dual-wf-{uuid4()}",
            task_queue=task_queue,
        )

    assert standalone == in_workflow == expected
