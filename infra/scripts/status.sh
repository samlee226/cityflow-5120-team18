#!/usr/bin/env bash
# Reports stack, instance and database state.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_stack

INSTANCE_ID="$(stack_output InstanceId)"

echo "Stack     : ${STACK_NAME} ($(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'Stacks[0].StackStatus' --output text))"
echo "Instance  : ${INSTANCE_ID} ($(aws ec2 describe-instances \
  --instance-ids "${INSTANCE_ID}" --region "${REGION}" \
  --query 'Reservations[0].Instances[0].State.Name' --output text))"
echo "Public IP : $(stack_output PublicIp)"
echo "Bucket    : $(stack_output RawDataBucketName)"
echo

ssh -o BatchMode=yes -o ConnectTimeout=8 -i "${KEY_FILE}" \
  "ubuntu@$(stack_output PublicIp)" \
  'cd cityflow/infra/compose 2>/dev/null && docker compose ps || echo "Compose stack not started yet."' \
  2>/dev/null || echo "Instance not reachable over SSH."
