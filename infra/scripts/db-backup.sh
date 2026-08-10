#!/usr/bin/env bash
# Writes a compressed dump to the raw data bucket. Worth running once the
# historical load finishes: regenerating that data takes hours, restoring it
# takes minutes.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_stack

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUCKET="$(stack_output RawDataBucketName)"

ssh -i "${KEY_FILE}" "ubuntu@$(stack_output PublicIp)" bash -s <<REMOTE
set -euo pipefail
cd ~/cityflow/infra/compose
set -a; source .env; set +a
docker compose exec -T db pg_dump -Fc -U "\${POSTGRES_USER}" -d "\${POSTGRES_DB}" \
  > /tmp/cityflow-${STAMP}.dump
aws s3 cp /tmp/cityflow-${STAMP}.dump s3://${BUCKET}/backups/
rm -f /tmp/cityflow-${STAMP}.dump
REMOTE

echo "Backup stored at s3://${BUCKET}/backups/cityflow-${STAMP}.dump"
