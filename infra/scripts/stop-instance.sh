#!/usr/bin/env bash
# Stops the instance to halt compute charges between demonstrations. The disk,
# the loaded database and the public address are all preserved.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_stack

aws ec2 stop-instances --instance-ids "$(stack_output InstanceId)" \
  --region "${REGION}" --query 'StoppingInstances[0].CurrentState.Name' --output text
echo "Live ingestion is paused while the instance is stopped."
