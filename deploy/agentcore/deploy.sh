#!/usr/bin/env bash
#
# Build the ARM64 image, push it to ECR, and print the runtime-creation command.
# Needs an AWS account with Bedrock and AgentCore access. Run from the repo root.
#
# Requires: docker with buildx, aws CLI logged in, AWS_REGION set.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

: "${AWS_REGION:?set AWS_REGION, e.g. export AWS_REGION=us-east-1}"
REPO_NAME="durable-agent-demo"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ECR="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${ECR}/${REPO_NAME}:latest"

echo "=== ensure ECR repo ==="
aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO_NAME" --region "$AWS_REGION" >/dev/null

echo "=== ECR login ==="
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR"

echo "=== build ARM64 and push ==="
# Build context is the repo root; the ignore file is deploy/agentcore/Dockerfile.dockerignore.
docker buildx build --platform linux/arm64 \
  -f deploy/agentcore/Dockerfile \
  -t "$IMAGE" --push .

echo ""
echo "pushed: $IMAGE"
echo ""
echo "Next, create the runtime (needs the execution role and Temporal Cloud config):"
echo "  export EXECUTION_ROLE_ARN=<ARN of the execution role you created; see deploy/agentcore/README.md>"
echo "  export TEMPORAL_ADDRESS=... TEMPORAL_NAMESPACE=... TEMPORAL_API_KEY=..."
echo "  IMAGE_URI=$IMAGE uv run python deploy/agentcore/create_runtime.py"
