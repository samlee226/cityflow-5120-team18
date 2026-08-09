#!/usr/bin/env bash
# Opens a shell on the instance.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_stack
exec ssh -i "${KEY_FILE}" "ubuntu@$(stack_output PublicIp)"
