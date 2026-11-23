#!/bin/sh
# Creates and migrates Temporal's own databases. Safe to run on every `up`.
set -eu
: "${POSTGRES_SEEDS:?}" "${POSTGRES_USER:?}" "${SQL_PASSWORD:?}"
PORT="${DB_PORT:-5432}"
tool() { temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" -u "$POSTGRES_USER" -p "$PORT" "$@"; }

until nc -z -w 5 "$POSTGRES_SEEDS" "$PORT"; do echo 'waiting for postgres'; sleep 2; done

for db in temporal temporal_visibility; do
  case "$db" in
    temporal) dir=/etc/temporal/schema/postgresql/v12/temporal/versioned ;;
    *) dir=/etc/temporal/schema/postgresql/v12/visibility/versioned ;;
  esac
  tool --db "$db" create 2>/dev/null || echo "database $db already exists"
  # Idempotent: skips when a schema version is already recorded, so an interrupted first run recovers.
  tool --db "$db" setup-schema -v 0.0
  tool --db "$db" update-schema -d "$dir"
done
echo 'temporal schema ready'
