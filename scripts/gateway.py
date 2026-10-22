"""Start the channel gateway on its own. Prints its inbound URL for a live curl."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

from channel.gateway import make_app  # noqa: E402
from durable_agent import config  # noqa: E402


async def _build_app():
    config.guard_runtime_host()
    client = await config.connect_client()
    return make_app(client)


def main() -> None:
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8090"))
    app = asyncio.run(_build_app())
    print(f"channel gateway on http://{host}:{port}", flush=True)
    print(f"  inbound:  POST http://{host}:{port}/inbound", flush=True)
    print(f"  outbound: POST http://{host}:{port}/outbound  (mock stub)", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
