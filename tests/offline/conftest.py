"""Offline test fixtures. No external network, no keys.

The SDK time-skipping test environment starts its own local server, which is
expected offline; what is forbidden is external network and any credential.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment


@pytest.fixture(autouse=True)
def _fast_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the mock latencies to near zero so the offline suite stays fast."""
    monkeypatch.setenv("MOCK_SKILL_LATENCY_MS", "0")
    monkeypatch.setenv("MOCK_MODEL_LATENCY_MS", "0")
    # Drain the ephemeral AgentCore worker quickly on the empty-payload path so the
    # serverless-worker tests do not wait the production debounce.
    monkeypatch.setenv("AGENTCORE_DEBOUNCE_SECONDS", "1")


# Enable the Standalone Activities flag on the local dev server so stage 1 tests
# can run a Standalone Activity, and register NextFollowUpAt, matching how run.sh
# starts the server.
_STANDALONE_FLAG = [
    "--dynamic-config-value",
    "activity.enableStandalone=true",
    "--search-attribute",
    "NextFollowUpAt=Datetime",
]


@pytest_asyncio.fixture(scope="session")
async def local_env():
    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=_STANDALONE_FLAG,
    ) as environment:
        yield environment


@pytest_asyncio.fixture
async def client(local_env):
    """Real local dev server: full features (id-conflict policy, standalone flag)."""
    return local_env.client


@pytest_asyncio.fixture
async def time_skip_env():
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        yield environment


@pytest_asyncio.fixture
async def time_skip_client(time_skip_env):
    """Time-skipping server for long-timer tests (beat 5)."""
    return time_skip_env.client
