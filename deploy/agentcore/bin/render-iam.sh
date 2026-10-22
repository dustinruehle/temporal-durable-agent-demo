#!/bin/bash
set -euo pipefail

# Fill in the IAM policy templates in deploy/agentcore/ for your AWS account and
# write them to deploy/agentcore/out/ (gitignored). The templates use the tokens
# PARTITION, REGION and ACCOUNT_ID so no account details live in the repo.
#
#   AWS_REGION=us-east-1 deploy/agentcore/bin/render-iam.sh
#
# AWS_PARTITION defaults to aws. ACCOUNT_ID comes from your current AWS credentials.

: "${AWS_REGION:?set AWS_REGION, for example export AWS_REGION=us-east-1}"
PARTITION="${AWS_PARTITION:-aws}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$DIR/out"

# The _comment key documents each template; IAM rejects unknown keys, so it is dropped.
for f in iam-execution-role-trust.json iam-execution-role-permissions.json iam-invoke-policy.json; do
  PARTITION="$PARTITION" REGION="$AWS_REGION" ACCOUNT_ID="$ACCOUNT_ID" python3 - "$DIR/$f" "$DIR/out/$f" <<'PY'
import json, os, sys
doc = json.load(open(sys.argv[1]))
doc.pop("_comment", None)
text = json.dumps(doc, indent=2)
for token in ("PARTITION", "REGION", "ACCOUNT_ID"):
    text = text.replace(f"arn:{token}:" if token == "PARTITION" else f":{token}:", (f"arn:{os.environ[token]}:" if token == "PARTITION" else f":{os.environ[token]}:"))
text = text.replace('"ACCOUNT_ID"', json.dumps(os.environ["ACCOUNT_ID"]))
open(sys.argv[2], "w").write(text + "\n")
PY
  echo "wrote deploy/agentcore/out/$f"
done
