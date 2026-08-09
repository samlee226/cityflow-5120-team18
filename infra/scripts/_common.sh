#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${CITYFLOW_STACK:-cityflow}"
REGION="${AWS_REGION:-ap-southeast-2}"
KEY_NAME="${CITYFLOW_KEY:-cityflow-ec2}"
KEY_FILE="${CITYFLOW_KEY_FILE:-${HOME}/.ssh/${KEY_NAME}.pem}"
TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/cloudformation/cityflow-infra.yaml"

stack_output() {
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

require_stack() {
  if ! aws cloudformation describe-stacks --stack-name "${STACK_NAME}" \
      --region "${REGION}" >/dev/null 2>&1; then
    echo "Stack '${STACK_NAME}' does not exist. Run scripts/deploy.sh first." >&2
    exit 1
  fi
}
