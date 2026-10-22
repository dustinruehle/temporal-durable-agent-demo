"""Beat 4: the session parks at an await across a gap. No process holds it.

Session lifetime is set by the business, not the runtime. After handling a
message the Workflow parks on a wait_condition: it stays RUNNING and queryable
with nothing scheduled, and a later message resumes it in the same execution.
"""

from __future__ import annotations

from temporalio.client import Client, WorkflowExecutionStatus

from durable_agent.config import build_plugin
from durable_agent.session_client import signal_with_start
from durable_agent.worker import running_workers
from durable_agent.workflows import SessionWorkflow
from tests.offline._harness import close_and_result, new_conversation_id, wait_for_processed


async def test_session_parks_then_resumes_in_same_execution(client: Client):
    plugin = build_plugin("mock")
    conv = new_conversation_id()
    async with running_workers(client, plugin):
        handle = await signal_with_start(client, conv, "buying under 15k", "happy")
        await wait_for_processed(handle, 1)

        # Parked: still RUNNING, nothing pending, no follow-up armed.
        desc = await handle.describe()
        assert desc.status == WorkflowExecutionStatus.RUNNING
        status = await handle.query(SessionWorkflow.status)
        assert status["pending"] == 0
        assert status["follow_up_armed"] is False

        # A later message resumes the same execution.
        run_id_before = handle.first_execution_run_id
        h2 = await signal_with_start(client, conv, "any SUVs?", "happy")
        assert h2.first_execution_run_id == run_id_before
        await wait_for_processed(handle, 2)

        result = await close_and_result(client, conv, handle)

    assert result["processed"] == 2
