#!/usr/bin/env bash
# Starts a stopped instance. Containers marked `restart: unless-stopped` come
# back automatically, and the ingestion timer catches up its missed run.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_stack

INSTANCE_ID="$(stack_output InstanceId)"
aws ec2 start-instances --instance-ids "${INSTANCE_ID}" --region "${REGION}" \
  --query 'StartingInstances[0].CurrentState.Name' --output text
aws ec2 wait instance-running --instance-ids "${INSTANCE_ID}" --region "${REGION}"
echo "Running at $(stack_output PublicIp)"
