#!/bin/bash
set -euo pipefail

# Creates the IAM role Temporal Cloud assumes to invoke your AgentCore runtime for
# Serverless Workers. It downloads the CloudFormation template that Temporal
# publishes in its docs and deploys it as a stack. The template holds the trust
# relationship to Temporal Cloud, so this repo does not copy it.
#
# The External ID is a value YOU choose (5 to 45 characters). Pass the SAME value
# here and to the Worker Deployment Version (serverless.sh wire reads EXTERNAL_ID).
#
# This is not the runtime's execution role; deploy/agentcore/README.md covers that.
#
# Get the runtime ARN from:
#   aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION"
# Append a trailing * so the role also covers the runtime's endpoints.

TEMPLATE_URL="https://docs.temporal.io/files/temporal-cloud-serverless-worker-agentcore-role.yaml"

STACK_NAME="${1:?Usage: mk-invoke-role.sh <stack-name> <external-id> <agent-runtime-arn>*}"
EXTERNAL_ID="${2:?Usage: mk-invoke-role.sh <stack-name> <external-id> <agent-runtime-arn>*}"
RUNTIME_ARN="${3:?Usage: mk-invoke-role.sh <stack-name> <external-id> <agent-runtime-arn>*}"
OUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/out"
mkdir -p "$OUT_DIR"
TEMPLATE="$OUT_DIR/temporal-cloud-serverless-worker-agentcore-role.yaml"

curl -fsSL "$TEMPLATE_URL" -o "$TEMPLATE"

aws cloudformation create-stack \
  --stack-name "$STACK_NAME" \
  --template-body "file://$TEMPLATE" \
  --parameters \
    ParameterKey=AssumeRoleExternalId,ParameterValue="$EXTERNAL_ID" \
    ParameterKey=AgentRuntimeARNs,ParameterValue="$RUNTIME_ARN" \
  --capabilities CAPABILITY_NAMED_IAM
aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME"
aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='RoleARN'].OutputValue" --output text
