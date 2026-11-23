#!/bin/sh
# One-shot: create or update the Agent Factory roles, then exit. Runs in the postgres image.
set -eu
: "${PGPASSWORD:?}" "${AF_OWNER_DB_PASSWORD:?}" "${AF_PERSONAL_DB_PASSWORD:?}" "${AF_SYNAPTION_DB_PASSWORD:?}"
exec psql -X -q -v ON_ERROR_STOP=1 -h "${PGHOST:-postgres}" -U "$PGUSER" -d agent_factory \
  -v owner_password="$AF_OWNER_DB_PASSWORD" \
  -v personal_password="$AF_PERSONAL_DB_PASSWORD" \
  -v synaption_password="$AF_SYNAPTION_DB_PASSWORD" \
  -f /af/roles.sql
