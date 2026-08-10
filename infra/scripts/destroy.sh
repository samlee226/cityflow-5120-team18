#!/usr/bin/env bash
# Deletes the stack and every resource in it, including the instance disk and
# the loaded database. The raw data bucket is retained.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_stack

echo "This deletes the instance and its database volume. Loaded data is lost"
echo "unless a backup has been taken. The S3 bucket is retained."
read -r -p "Type the stack name to continue: " CONFIRMATION
[[ "${CONFIRMATION}" == "${STACK_NAME}" ]] || { echo "Cancelled."; exit 1; }

aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${REGION}"
echo "Stack deleted."
