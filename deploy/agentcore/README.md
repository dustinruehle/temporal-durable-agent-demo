# Run the Worker on Amazon Bedrock AgentCore Runtime

> **Release stage: Pre-release.** [Serverless Workers on Amazon Bedrock AgentCore Runtime](https://docs.temporal.io/serverless-workers/agentcore)
> are in Pre-release, and their APIs may change in backwards-incompatible ways. On Temporal Cloud,
> [request access](https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers)
> through a support ticket or your account team.

This folder hosts the same session Workflow inside AgentCore Runtime. No Worker runs between
calls. Each invocation starts a short-lived Worker in the AgentCore microVM, runs the turn, and
drains. The session and its state stay in Temporal Cloud, so AgentCore can reap the idle microVM
and the next invocation resumes the same conversation.

You can skip this folder entirely. The [main README](../../README.md) runs everything on your
machine.

## Contents

1. [How it works](#how-it-works)
2. [What you need](#what-you-need)
3. [Step 1: Create the execution role](#step-1-create-the-execution-role)
4. [Step 2: Build and push the image](#step-2-build-and-push-the-image)
5. [Step 3: Create the runtime](#step-3-create-the-runtime)
6. [Step 4: Invoke it with no local Worker](#step-4-invoke-it-with-no-local-worker)
7. [Step 5 (optional): Let Temporal start the Worker](#step-5-optional-let-temporal-start-the-worker)
8. [Files in this folder](#files-in-this-folder)
9. [Known limits](#known-limits)

## How it works

![AgentCore deployment: what runs where](../../docs/agentcore-what-runs-where.svg)

- **The container** ([`Dockerfile`](Dockerfile)) runs [`scripts/runtime.py`](../../scripts/runtime.py).
  It serves the AgentCore contract (`POST /invocations`, `GET /ping` on port 8080) through the
  AgentCore SDK app in [`src/runtime/app.py`](../../src/runtime/app.py).
- **Each invocation** starts an ephemeral Worker on both Task Queues. With a message in the
  payload, it delivers one turn by Signal-With-Start and returns the reply. With an empty payload
  (the path Temporal uses), it polls until the queue is idle, then drains.
- **The runtime session id is the Workflow Id.** AgentCore requires a session id of at least 33
  characters. A later invocation with the same id resumes the same conversation.
- **Drain on idle.** [`src/runtime/idle.py`](../../src/runtime/idle.py) holds an Activity tracker
  interceptor. It sees every model call and tool call, so the Worker does not stop mid turn.
  `/ping` reports `HealthyBusy` while a turn runs and `Healthy` once the work is in Temporal.
- **Network mode is `PUBLIC`.** The container reaches Temporal Cloud on port 7233 over the
  managed outbound path, with no VPC or NAT. Choose `VPC` mode only if your policy requires it;
  it then needs private subnets, a NAT gateway and an egress rule for `7233/tcp`.

## What you need

- An AWS account where you can use Amazon Bedrock, Amazon ECR and AgentCore Runtime. An
  organization Service Control Policy that denies `ecr` or `bedrock-agentcore` blocks the deploy.
- Bedrock model access for the model you pick. Strands calls the Bedrock ConverseStream API; for
  Anthropic models, submit the Anthropic use-case form in the Bedrock console first.
- Docker with `buildx` (the image is `linux/arm64`), the AWS CLI and `uv`.
- A Temporal Cloud Namespace on AWS and an API key: `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`,
  `TEMPORAL_API_KEY`. Put them in the repo `.env` (see [`.env.example`](../../.env.example)).

## Step 1: Create the execution role

AgentCore assumes this role to run the container. The repo holds the trust policy and the
permissions as templates with the tokens `PARTITION`, `REGION` and `ACCOUNT_ID`. Render them for
your account:

```bash
export AWS_REGION=us-east-1
deploy/agentcore/bin/render-iam.sh        # writes deploy/agentcore/out/*.json (gitignored)
```

Create the role from `out/iam-execution-role-trust.json` and attach
`out/iam-execution-role-permissions.json`. For example:

```bash
aws iam create-role --role-name DurableAgentDemoExecution \
  --assume-role-policy-document file://deploy/agentcore/out/iam-execution-role-trust.json
aws iam put-role-policy --role-name DurableAgentDemoExecution --policy-name runtime \
  --policy-document file://deploy/agentcore/out/iam-execution-role-permissions.json
export EXECUTION_ROLE_ARN="$(aws iam get-role --role-name DurableAgentDemoExecution --query Role.Arn --output text)"
```

Give whoever invokes the runtime the permissions in `out/iam-invoke-policy.json`.

## Step 2: Build and push the image

```bash
./deploy/agentcore/deploy.sh   # creates the ECR repo if needed, builds ARM64, pushes, prints IMAGE_URI
```

## Step 3: Create the runtime

```bash
set -a; . ./.env; set +a       # TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_API_KEY
export MODEL_MODE=bedrock BEDROCK_MODEL_ID=<a Bedrock model or inference profile id you can use>
IMAGE_URI=<from step 2> uv run python deploy/agentcore/create_runtime.py
# prints agentRuntimeArn
```

The runtime uses a 15-minute idle timeout and an 8-hour maximum lifetime. Keep the API key in
plain env for a test only; for anything longer, inject it from AWS Secrets Manager (the
permissions template has an optional statement for it).

## Step 4: Invoke it with no local Worker

```bash
ARN=<agentRuntimeArn from step 3>
SID=session-$(date +%s)-pad-abcdefghijklmnop   # 33 characters or more; reuse it across turns
aws bedrock-agentcore invoke-agent-runtime --region "$AWS_REGION" \
  --agent-runtime-arn "$ARN" --runtime-session-id "$SID" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"prompt":"I am looking to buy under 15k","scenario":"happy"}' \
  out.json && cat out.json
```

Invoke again with the same `SID` and a follow-up such as "book the first one". The reply knows
the first turn. To prove the microVM went away between turns, stop the session first:

```bash
aws bedrock-agentcore stop-runtime-session --region "$AWS_REGION" \
  --agent-runtime-arn "$ARN" --runtime-session-id "$SID"
```

The next invoke starts a fresh microVM, and the conversation continues. In the Temporal UI, the
Workflow Id is the value of `SID`.

## Step 5 (optional): Let Temporal start the Worker

With a compute provider on a Worker Deployment Version, Temporal's Worker Controller Instance
invokes the runtime when a Task waits and no Worker polls. You then never call
`invoke-agent-runtime` yourself. See
[how Serverless invocation works](https://docs.temporal.io/serverless-workers#how-invocation-works).

1. **Invoke role.** The role Temporal Cloud assumes to call the runtime. The script downloads
   [Temporal's CloudFormation template](https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/agentcore)
   and deploys it. The External ID is a value you choose (5 to 45 characters); the same value
   goes to the Worker Deployment Version.

   ```bash
   deploy/agentcore/bin/mk-invoke-role.sh temporal-agentcore-invoke <external-id> "<runtime-arn>*"
   ```

2. **Versioned runtime.** Serverless Workers need Worker Versioning. This sets
   `TEMPORAL_DEPLOYMENT_NAME` and `TEMPORAL_BUILD_ID` on the live runtime, with no image rebuild
   ([`config.worker_deployment_config()`](../../src/durable_agent/config.py#L118) reads them):

   ```bash
   RUNTIME_ID=<your runtime id> DEPLOYMENT_NAME=durable-agent-demo BUILD_ID=v1 \
     uv run python deploy/agentcore/set_runtime_versioning.py
   ```

3. **Compute provider on the version, then prove it.**

   ```bash
   export RUNTIME_ID=<your runtime id> EXTERNAL_ID=<external-id> INVOKE_STACK=temporal-agentcore-invoke
   deploy/agentcore/serverless.sh wire
   uv run python scripts/verify_serverless.py "I am looking for an SUV under 25k"
   # PASS: the turn completed with no local Worker
   ```

4. **Turn it off** when you go back to the local beats. A current version routes the
   `agent-session` queue to versioned Workers only, so an unversioned local Worker gets no new
   sessions while it is wired.

   ```bash
   deploy/agentcore/serverless.sh unwire
   RUNTIME_ID=<your runtime id> uv run python deploy/agentcore/set_runtime_versioning.py --clear
   ```

   After a run, the version lists pollers for a few minutes and does not delete. Rerun
   `serverless.sh unwire` once they age out.

## Files in this folder

| File | What it does |
|---|---|
| [`Dockerfile`](Dockerfile) | ARM64 image that runs `scripts/runtime.py` |
| [`deploy.sh`](deploy.sh) | Creates the ECR repo, builds, pushes, prints the next command |
| [`create_runtime.py`](create_runtime.py) | `CreateAgentRuntime` with `PUBLIC` network mode and the Temporal env |
| [`iam-execution-role-trust.json`](iam-execution-role-trust.json), [`iam-execution-role-permissions.json`](iam-execution-role-permissions.json) | Execution role templates |
| [`iam-invoke-policy.json`](iam-invoke-policy.json) | Lets a caller invoke and stop runtime sessions |
| [`bin/render-iam.sh`](bin/render-iam.sh) | Fills the templates for your account into `out/` |
| [`bin/mk-invoke-role.sh`](bin/mk-invoke-role.sh) | Deploys Temporal's invoke-role template |
| [`set_runtime_versioning.py`](set_runtime_versioning.py) | Turns Worker Versioning on or off on the runtime |
| [`serverless.sh`](serverless.sh) | `status`, `wire`, `bootstrap`, `unwire` for the compute provider |

## Known limits

- **First call to a cold runtime.** The first invocation can return a 500 on the response path
  while the microVM starts, because the app waits for the reply. The turn is durable either way;
  the next invocation returns it.
- **Dashboard lanes.** A Workflow Query needs a running Worker. With no persistent Worker, the
  dashboard's WORKER lane only reads during an invocation. For the dashboard beats, run a local
  Worker (`scripts/worker.py`).
- **Environment variable field.** `create_runtime.py` passes the container env in
  `environmentVariables`. If your AgentCore API version rejects it, inject the `TEMPORAL_*`
  values through Secrets Manager or the runtime configuration.
