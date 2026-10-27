#!/usr/bin/env bash
# Deploy / remove the OCR pipeline stack.
#
#   ./deploy.sh --profile my-poc --region ap-northeast-2            # synth (+cdk-nag), confirm, deploy
#   ./deploy.sh --profile my-poc --region ap-northeast-2 --diff     # show changes only
#   ./deploy.sh --profile my-poc --region ap-northeast-2 --destroy  # delete the stack and its data
#   extra CDK context:  -c modelId=global.anthropic.claude-opus-5-5 -c reviewTtlDays=3
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="" REGION="" ACTION=deploy YES=false CONTEXT=()
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    --diff)    ACTION=diff; shift ;;
    --destroy) ACTION=destroy; shift ;;
    --yes)     YES=true; shift ;;
    -c)        CONTEXT+=(-c "$2"); shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
[ -n "$PROFILE" ] && [ -n "$REGION" ] || { echo "Usage: ./deploy.sh --profile <name> --region <region> [--diff|--destroy] [--yes]" >&2; exit 1; }

# Pinned toolchain: Python CDK app in infra/.venv, CDK CLI from package.json.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
[ -x node_modules/.bin/cdk ] || npm install --no-fund --no-audit --silent
CDK=(node_modules/.bin/cdk --profile "$PROFILE" --region "$REGION" "${CONTEXT[@]+"${CONTEXT[@]}"}")
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" CDK_DEFAULT_REGION="$REGION"

ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
echo "Account $ACCOUNT  Region $REGION  Profile $PROFILE  Action $ACTION"

confirm() {
  $YES && return 0
  read -r -p "$1 [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || { echo "Cancelled."; exit 1; }
}

case "$ACTION" in
  diff)
    "${CDK[@]}" diff --method=template ;;
  destroy)
    confirm "Delete the stack, its buckets (all scans and results) and tables in $ACCOUNT/$REGION?"
    "${CDK[@]}" destroy --force ;;
  deploy)
    # Bootstrap once per account/Region (no-op when it already exists).
    aws cloudformation describe-stacks --stack-name CDKToolkit --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1 \
      || { confirm "CDK is not bootstrapped in $ACCOUNT/$REGION. Bootstrap now?"; "${CDK[@]}" bootstrap "aws://$ACCOUNT/$REGION"; }
    "${CDK[@]}" synth --quiet          # fails on any unacknowledged cdk-nag finding
    "${CDK[@]}" diff --method=template || true
    confirm "Deploy these changes to $ACCOUNT/$REGION?"
    "${CDK[@]}" deploy --require-approval never --outputs-file .build/outputs.json
    echo
    echo "Next: upload a schema and a scan with the local client, e.g."
    echo "  ../run.sh pipeline schema ../data/schema/<name>.json --type <doc-type>"
    echo "  ../run.sh pipeline upload ../data/images/<name>.png --type <doc-type>"
    ;;
esac
