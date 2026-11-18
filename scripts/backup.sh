#!/usr/bin/env bash
# Back up the NOVALYN database: one compressed pg_dump file, optionally copied to S3.
#
#   DATABASE_URL=postgresql://user:password@host:5432/novalyn scripts/backup.sh [output-directory]
#
# Optional:
#   BACKUP_S3_URI=s3://my-backups/novalyn/   copy the file there with the AWS CLI (bucket with
#                                            versioning, encryption and a lifecycle rule for retention)
#
# Uploaded files live in the document bucket, not in the database. Protect that bucket with
# versioning and, ideally, replication to another region; see docs/DEPLOYMENT.md.
set -euo pipefail

: "${DATABASE_URL:?Set DATABASE_URL to the database to back up}"
url="${DATABASE_URL/postgresql+psycopg:/postgresql:}"
out_dir="${1:-./backups}"
mkdir -p "$out_dir"
file="$out_dir/novalyn-$(date -u +%Y%m%dT%H%M%SZ).dump"

# Tenant tables use forced Row-Level Security, which pg_dump refuses to work around on its own.
# Dump with row security on, in the system scope the policies allow (this session only).
export PGOPTIONS="${PGOPTIONS:-} -c app.rls_bypass=on"
# Custom format: compressed, and restorable table by table with pg_restore.
pg_dump --format=custom --no-owner --no-privileges --enable-row-security --file="$file" "$url"
pg_restore --list "$file" >/dev/null # fails if the file is unreadable
echo "Backup written: $file ($(du -h "$file" | cut -f1))"

if [[ -n "${BACKUP_S3_URI:-}" ]]; then
  aws s3 cp "$file" "${BACKUP_S3_URI%/}/$(basename "$file")" --only-show-errors
  echo "Copied to ${BACKUP_S3_URI%/}/$(basename "$file")"
fi
