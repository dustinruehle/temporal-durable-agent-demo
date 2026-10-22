"""scripts/worker.py stops on Ctrl-C and on kill.

run.sh starts the Worker in the background, and a shell starts background jobs
with SIGINT ignored. The Worker installs its own handlers, so SIGINT stops it in
both cases. SIGTERM is what run.sh sends on cleanup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXIT_WITHIN_SECONDS = 10.0


def _start_worker(target: str, ignore_sigint: bool) -> subprocess.Popen:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TEMPORAL_", "AWS_", "ANTHROPIC_", "BEDROCK_"))
    }
    env.update(
        PYTHONPATH=str(REPO / "src"),
        TEMPORAL_ADDRESS=target,
        TEMPORAL_NAMESPACE="default",
        # Keep a local profile file from pointing the Worker anywhere else.
        TEMPORAL_CONFIG_FILE=str(REPO / "tests" / "offline" / "no-such-profile.toml"),
        MODEL_MODE="mock",
        SKILLS_MODE="mock",
        RUNTIME_HOST="local",
    )

    def preexec() -> None:
        if ignore_sigint:
            signal.signal(signal.SIGINT, signal.SIG_IGN)

    return subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "worker.py")],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=preexec,
    )


def _wait_for_up(proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if "worker up" in line:
            return
        if not line and proc.poll() is not None:
            break
    proc.kill()
    raise AssertionError("worker did not start")


@pytest.mark.parametrize(
    ("signum", "ignore_sigint"),
    [
        (signal.SIGINT, False),  # Ctrl-C in its own terminal
        (signal.SIGINT, True),  # Ctrl-C reaching a Worker started by run.sh
        (signal.SIGTERM, True),  # run.sh cleanup, or kill <pid>
    ],
)
async def test_worker_exits_on_signal(client, signum: int, ignore_sigint: bool) -> None:
    target = client.service_client.config.target_host
    proc = _start_worker(target, ignore_sigint)
    try:
        _wait_for_up(proc)
        time.sleep(1.0)  # let the pollers start
        sent = time.monotonic()
        proc.send_signal(signum)
        try:
            proc.wait(timeout=EXIT_WITHIN_SECONDS)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"worker still running {EXIT_WITHIN_SECONDS:g}s after {signal.Signals(signum).name}"
            ) from None
        assert time.monotonic() - sent < EXIT_WITHIN_SECONDS
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
