#!/usr/bin/env bash
# Forwards the remote database to localhost:5432 for the duration of the
# session, so tools configured for a local database work unchanged.
# Leave this running in its own terminal.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_stack

echo "Database available at localhost:5432 while this session is open."
echo "Press Ctrl-C to close."
exec ssh -N -L 5432:localhost:5432 -i "${KEY_FILE}" "ubuntu@$(stack_output PublicIp)"
