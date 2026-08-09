#!/usr/bin/env bash
# Changes the instance size and waits until the database is serving again.
#
# CloudFormation stops, modifies and restarts the instance as part of the
# update. The disk, the loaded database and the public address are preserved;
# the host is unreachable for a few minutes while it restarts.
#
#   ./resize.sh t3.small    before the historical pipeline runs
#   ./resize.sh t3.micro    once the data is loaded
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_stack

TARGET="${1:-}"
case "${TARGET}" in
  t3.micro|t3.small|t3.medium) ;;
  *) echo "Usage: resize.sh <t3.micro|t3.small|t3.medium>" >&2; exit 1 ;;
esac

INSTANCE_ID="$(stack_output InstanceId)"
CURRENT="$(aws ec2 describe-instances --instance-ids "${INSTANCE_ID}" \
  --region "${REGION}" --query 'Reservations[0].Instances[0].InstanceType' \
  --output text)"

if [[ "${CURRENT}" == "${TARGET}" ]]; then
  echo "Already running as ${TARGET}."
  exit 0
fi

echo "Resizing ${CURRENT} -> ${TARGET}. The host restarts and is briefly"
echo "unavailable. Confirm nobody is mid-task before continuing."
read -r -p "Continue? [y/N] " REPLY
[[ "${REPLY}" == "y" || "${REPLY}" == "Y" ]] || { echo "Cancelled."; exit 1; }

"$(dirname "${BASH_SOURCE[0]}")/deploy.sh" "${TARGET}"
aws ec2 wait instance-running --instance-ids "${INSTANCE_ID}" --region "${REGION}"

# EC2 reports the instance as running well before the containers have
# restarted, so wait for the database itself rather than the instance state.
HOST="$(stack_output PublicIp)"
echo -n "Waiting for the database"
for _ in $(seq 1 60); do
  if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
      -i "${KEY_FILE}" "ubuntu@${HOST}" \
      'cd cityflow/infra/compose && docker compose exec -T db pg_isready -q' \
      >/dev/null 2>&1; then
    echo
    echo "Running as ${TARGET}; database accepting connections at ${HOST}."
    exit 0
  fi
  echo -n "."
  sleep 10
done

echo
echo "Instance is ${TARGET} but the database did not respond within 10 minutes." >&2
echo "Check with: ./scripts/status.sh" >&2
exit 1
