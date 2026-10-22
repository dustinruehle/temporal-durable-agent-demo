"""Serve the AgentCore runtime as a Temporal Serverless Worker.

This is the container entrypoint and the local launcher. It connects a
client through the same env-config path the rest of the demo uses (local dev server
or Temporal Cloud, a pure env change) and serves the AgentCore contract on
0.0.0.0:8080, the port AgentCore requires.

There is no persistent Worker: each invocation starts an ephemeral Worker that drains
when idle (see runtime/app.py). Temporal (once a compute provider is configured) or a
manual invoke is what starts a Worker on demand, so nothing polls when the runtime is
idle.
"""

from __future__ import annotations

import asyncio

import uvicorn

from durable_agent import config
from runtime.app import make_app

HOST = "0.0.0.0"
PORT = 8080


async def _serve() -> None:
    config.validate_model_credentials()
    client = await config.connect_client()
    plugin = config.build_plugin()
    app = make_app(client, plugin, deployment_config=config.worker_deployment_config())
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="info", access_log=False)
    )
    versioned = "versioned" if config.worker_deployment_config() is not None else "unversioned"
    print(
        f"agentcore serverless worker on http://{HOST}:{PORT} "
        f"(target={config.temporal_target()} model={config.model_mode()} {versioned}); "
        "POST /invocations, GET /ping",
        flush=True,
    )
    await server.serve()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
