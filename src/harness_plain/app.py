"""Run the stage 0 harness feed. Startable on its own for beats 1 and 2."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python src/harness_plain/app.py` as well as `-m harness_plain.app`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from domain.harness_feed import make_app  # noqa: E402
from harness_plain.loop import Harness  # noqa: E402

app = make_app(Harness())


def main() -> None:
    port = int(os.environ.get("HARNESS_PORT", "8091"))
    print(f"stage 0 harness feed on http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
