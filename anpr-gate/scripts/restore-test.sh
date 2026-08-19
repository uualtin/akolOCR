#!/bin/sh
set -eu

archive_root=${ARCHIVE_ROOT:-/archive}
backup=$(find "$archive_root/backups/postgres/daily" -type f -name '*.sql.gz' -print | sort | tail -n 1)
if [ -z "$backup" ]; then
  echo "No PostgreSQL backup found" >&2
  exit 1
fi

admin_password_file=${POSTGRES_ADMIN_PASSWORD_FILE:-/run/secrets/postgres_admin_password}
export PGPASSWORD
PGPASSWORD=$(tr -d '\r\n' < "$admin_password_file")
host=${DATABASE_HOST:-postgres}
port=${DATABASE_PORT:-5432}
test_database="anpr_restore_test_$(date +%Y%m%d%H%M%S)"
restore_sql=$(mktemp)

cleanup() {
  dropdb --if-exists --force --host "$host" --port "$port" --username postgres "$test_database"
  rm -f "$restore_sql"
}
trap cleanup EXIT INT TERM

gzip -t "$backup"
gzip -dc "$backup" > "$restore_sql"
createdb --host "$host" --port "$port" --username postgres "$test_database"
psql --host "$host" --port "$port" --username postgres --dbname "$test_database" --set=ON_ERROR_STOP=1 --file "$restore_sql"
verified=$(psql --host "$host" --port "$port" --username postgres --dbname "$test_database" --tuples-only --no-align --command \
  "SELECT to_regclass('public.gate_events') IS NOT NULL AND to_regclass('public.access_entries') IS NOT NULL;")
if [ "$verified" != "t" ]; then
  echo "Restored database schema verification failed" >&2
  exit 1
fi
echo "Restore test passed: $backup"
