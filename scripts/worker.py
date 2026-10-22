"""Start the stage 2 workers. Startable alone so beat 7 can add a second replica.

  python scripts/worker.py                 agent-session worker + one model-calls replica
  python scripts/worker.py --replica-only   an extra model-calls replica only (beat 7)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from durable_agent.worker import main  # noqa: E402


def cli() -> None:
    parser = argparse.ArgumentParser(description="stage 2 workers")
    parser.add_argument(
        "--replica-only",
        action="store_true",
        help="run only a model-calls replica (no session worker), for beat 7",
    )
    args = parser.parse_args()
    asyncio.run(main(model_replicas=1, include_agent_worker=not args.replica_only))


if __name__ == "__main__":
    cli()
