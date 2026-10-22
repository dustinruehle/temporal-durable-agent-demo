# AgentCore Runtime hosts this container. It MUST be linux/arm64 (Graviton), so
# build it with buildx: docker buildx build --platform linux/arm64 ... --push .
#
# The container is the same app you run locally with scripts/runtime.py: it starts the
# Temporal Worker in-process and serves the AgentCore contract on 0.0.0.0:8080.
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Dependencies first, for layer caching. The lockfile pins exact versions.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Application code. datasets/ holds the scenario profiles the domain reads.
COPY src ./src
COPY scripts ./scripts
COPY datasets ./datasets

# The demo imports from src/ via PYTHONPATH (a non-package src layout).
ENV PYTHONPATH=/app/src

# AgentCore calls the container on this port; it is not configurable.
EXPOSE 8080

# Start the Temporal Worker in-process and serve POST /invocations + GET /ping.
CMD ["uv", "run", "--no-dev", "python", "scripts/runtime.py"]
