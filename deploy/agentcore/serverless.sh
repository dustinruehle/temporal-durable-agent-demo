#!/usr/bin/env bash
#
# Wire (or tear down) the AgentCore runtime as a Temporal Serverless Worker, so
# Temporal Cloud invokes the runtime on demand with no persistent Worker.
#
#   serverless.sh status     show the deployment, its version, and the compute config
#   serverless.sh wire        create the deployment + version with the AgentCore
#                             compute provider, and set it current (no pollers needed)
#   serverless.sh bootstrap   one direct invoke so the version registers the demo's
#                             task queues (run once, after the runtime is versioned)
#   serverless.sh unwire      make unversioned current, delete the version + deployment
#
# The runtime itself is made versioned separately (no image rebuild):
#   DEPLOYMENT_NAME=$DEPLOYMENT_NAME BUILD_ID=$BUILD_ID \
#     uv run python deploy/agentcore/set_runtime_versioning.py
#
# Config (env overrides, else these defaults):
#   DEPLOYMENT_NAME  durable-agent-demo      Worker Deployment name
#   BUILD_ID         v1                      Worker Deployment Version build id
#   EXTERNAL_ID      (required)              the External ID you chose for the invoke
#                                            role; it must match on both sides
#   RUNTIME_ID       (required)              your AgentCore runtime id (from
#                                            create_runtime.py or list-agent-runtimes)
#   INVOKE_STACK     temporal-agentcore-invoke  CloudFormation stack of the invoke role
#   ASSUME_ROLE_ARN  (from the stack output) the role Temporal assumes to invoke
#
# Temporal Cloud connection and AWS_REGION come from the repo .env.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
if [ -f ./.env ]; then set -a; . ./.env; set +a; fi

DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-durable-agent-demo}"
BUILD_ID="${BUILD_ID:-v1}"
: "${RUNTIME_ID:?set RUNTIME_ID to your AgentCore runtime id}"
EXTERNAL_ID="${EXTERNAL_ID:-}"
INVOKE_STACK="${INVOKE_STACK:-temporal-agentcore-invoke}"
REGION="${AWS_REGION:-us-east-1}"

T=(temporal worker deployment --address "$TEMPORAL_ADDRESS" --namespace "$TEMPORAL_NAMESPACE" --api-key "$TEMPORAL_API_KEY")

runtime_arn() {
  aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
    --agent-runtime-id "$RUNTIME_ID" --query agentRuntimeArn --output text
}
endpoint_arn() { echo "$(runtime_arn)/runtime-endpoint/DEFAULT"; }

assume_role_arn() {
  if [[ -n "${ASSUME_ROLE_ARN:-}" ]]; then echo "$ASSUME_ROLE_ARN"; return; fi
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$INVOKE_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='RoleARN'].OutputValue" --output text
}

cmd_status() {
  echo "=== deployment: $DEPLOYMENT_NAME ==="
  "${T[@]}" describe --name "$DEPLOYMENT_NAME" 2>&1 || echo "  (not found)"
  echo "=== version: $BUILD_ID ==="
  "${T[@]}" describe-version --deployment-name "$DEPLOYMENT_NAME" --build-id "$BUILD_ID" 2>&1 || echo "  (not found)"
}

cmd_wire() {
  : "${EXTERNAL_ID:?set EXTERNAL_ID to the External ID you chose for the invoke role}"
  local ep role
  ep="$(endpoint_arn)"; role="$(assume_role_arn)"
  echo "deployment=$DEPLOYMENT_NAME build=$BUILD_ID"
  echo "endpoint=$ep"
  echo "assume-role=$role  external-id=$EXTERNAL_ID"
  echo "--- create deployment (ok if it already exists) ---"
  "${T[@]}" create --name "$DEPLOYMENT_NAME" 2>&1 || true
  echo "--- create version with AgentCore compute provider ---"
  "${T[@]}" create-version \
    --deployment-name "$DEPLOYMENT_NAME" --build-id "$BUILD_ID" \
    --aws-agentcore-endpoint-arn "$ep" \
    --aws-agentcore-assume-role-arn "$role" \
    --aws-agentcore-assume-role-external-id "$EXTERNAL_ID"
  echo "--- set current (no live pollers required for a serverless version) ---"
  "${T[@]}" set-current-version \
    --deployment-name "$DEPLOYMENT_NAME" --build-id "$BUILD_ID" \
    --allow-no-pollers --ignore-missing-task-queues --yes
  echo "OK. Run 'serverless.sh status' to confirm."
}

cmd_bootstrap() {
  # One direct invoke (empty payload) so the versioned Worker registers the demo's
  # task queues under this deployment version. After this, WCI can invoke on its own.
  local arn sid out
  arn="$(runtime_arn)"
  sid="serverless-bootstrap-$(printf '%033d' 1)"   # >= 33 chars, as AgentCore requires
  out="$(mktemp)"
  echo "bootstrap invoke (empty payload) -> $arn"
  aws bedrock-agentcore invoke-agent-runtime --region "$REGION" \
    --agent-runtime-arn "$arn" --runtime-session-id "$sid" \
    --content-type application/json --accept application/json \
    --payload '{}' --cli-binary-format raw-in-base64-out "$out"
  echo "response: $(cat "$out")"; rm -f "$out"
}

cmd_unwire() {
  echo "--- make unversioned current ---"
  "${T[@]}" set-current-version --deployment-name "$DEPLOYMENT_NAME" \
    --unversioned --allow-no-pollers --ignore-missing-task-queues --yes 2>&1 || true
  echo "--- delete version ---"
  "${T[@]}" delete-version --deployment-name "$DEPLOYMENT_NAME" --build-id "$BUILD_ID" --skip-drainage 2>&1 || true
  echo "--- delete deployment ---"
  "${T[@]}" delete --name "$DEPLOYMENT_NAME" 2>&1 || true
  echo "Done. Runtime env is cleared separately: set_runtime_versioning.py --clear"
}

case "${1:-status}" in
  status) cmd_status ;;
  wire) cmd_wire ;;
  bootstrap) cmd_bootstrap ;;
  unwire) cmd_unwire ;;
  *) echo "usage: serverless.sh {status|wire|bootstrap|unwire}" >&2; exit 2 ;;
esac
