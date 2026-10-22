"""Run the AgentCore runtime app locally.

Starts the Temporal Worker in-process and serves the AgentCore contract on
0.0.0.0:8080. Point it at a local dev server or Temporal Cloud through .env, the
same way run.sh does. This is also the process the container runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtime.serve import main  # noqa: E402

if __name__ == "__main__":
    main()
