#!/usr/bin/env bash
# Creates or updates the infrastructure stack. Safe to re-run: CloudFormation
# applies only the difference, and rolls back if a change fails.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

INSTANCE_TYPE="${1:-t3.small}"
SSH_CIDR="${CITYFLOW_SSH_CIDR:-0.0.0.0/0}"
UPLOADER_ARN="${CITYFLOW_UPLOADER_ARN:-}"

aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --template-file "${TEMPLATE}" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    KeyPairName="${KEY_NAME}" \
    InstanceType="${INSTANCE_TYPE}" \
    SshCidr="${SSH_CIDR}" \
    DataUploaderArn="${UPLOADER_ARN}"

echo
echo "Instance type : ${INSTANCE_TYPE}"
echo "Public IP     : $(stack_output PublicIp)"
echo "Raw data      : $(stack_output RawDataPrefix)"
echo "SSH           : $(stack_output SshCommand)"
echo
echo "First boot installs Docker and the AWS CLI; allow a few minutes before"
echo "connecting."
