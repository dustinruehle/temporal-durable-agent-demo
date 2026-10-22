"""Print the resolved switches and supporting config, and the run mode.

Used by Preflight and run.sh so a presenter can see at a glance what is running.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain import config as dconfig  # noqa: E402
from durable_agent import config as sconfig  # noqa: E402


def main() -> None:
    target = sconfig.temporal_target()
    model = sconfig.model_mode()
    skills = sconfig.skills_mode()
    runtime = sconfig.runtime_host()

    print("switches:")
    print(f"  TEMPORAL_TARGET = {target}")
    print(f"  MODEL_MODE      = {model}")
    print(f"  SKILLS_MODE     = {skills}")
    print(f"  RUNTIME_HOST    = {runtime}")
    print("supporting config:")
    print(f"  MODEL_QUEUE_RPS       = {dconfig.model_queue_rps()}")
    print(f"  FOLLOW_UP_DELAY       = {dconfig.follow_up_seconds()} seconds")
    print(f"  MOCK_SKILL_LATENCY_MS = {dconfig.skill_latency_ms()}")
    print(f"  MOCK_MODEL_LATENCY_MS = {dconfig.model_latency_ms()}")
    print(f"  TASK_QUEUE_AGENT      = {dconfig.TASK_QUEUE_AGENT}")
    print(f"  TASK_QUEUE_MODEL      = {dconfig.TASK_QUEUE_MODEL}")

    mode = "mock model, mock skills, local dev server"
    if model != "mock" or skills != "mock" or target != "local":
        mode = f"model={model}, skills={skills}, target={target}"
    print(f"run mode if launched now: {mode}")
    if runtime == "agentcore":
        print("NOTE: RUNTIME_HOST=agentcore runs through scripts/runtime.py, not run.sh.")


if __name__ == "__main__":
    main()
