#!/usr/bin/env bash
# Restore a backup made by backup.sh into an EMPTY database, then check it.
#
#   DATABASE_URL=postgresql://novalyn_app@host:5432/novalyn_restore scripts/restore.sh backups/novalyn-....dump
#
# Restore as the role the application connects with, so that it owns the tables and the
# Row-Level Security policies (which the dump contains) apply to it. Never restore over the
# live database: restore next to it, check, then switch the application's DATABASE_URL.
set -euo pipefail

: "${DATABASE_URL:?Set DATABASE_URL to the empty database to restore into}"
file="${1:?Give the backup file to restore}"
url="${DATABASE_URL/postgresql+psycopg:/postgresql:}"

tables=$(psql "$url" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [[ "$tables" != "0" ]]; then
  echo "The target database is not empty ($tables tables). Restore into a new database." >&2
  exit 1
fi

# The restored tables have forced Row-Level Security: load the rows in the system scope.
export PGOPTIONS="${PGOPTIONS:-} -c app.rls_bypass=on"
pg_restore --no-owner --no-privileges --enable-row-security --single-transaction --exit-on-error \
  --dbname="$url" "$file"

echo "Restored. Checks:"
psql "$url" -Atc "SELECT 'schema version: ' || version_num FROM alembic_version"
psql "$url" -Atc "SELECT 'organizations: ' || count(*) FROM organizations"
PGOPTIONS="-c app.rls_bypass=on" psql "$url" -Atc "SELECT 'memberships: ' || count(*) FROM memberships"
psql "$url" -Atc "SELECT 'tables with row security: ' || count(*) FROM pg_class WHERE relrowsecurity AND relforcerowsecurity"
echo "Point a staging copy of the application at it and sign in before switching production."
