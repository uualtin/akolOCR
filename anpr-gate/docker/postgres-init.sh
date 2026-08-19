set -eu

app_password=$(tr -d '\r\n' < /run/secrets/database_password)

psql --username "$POSTGRES_USER" --dbname postgres \
  --set=ON_ERROR_STOP=1 --set=app_password="$app_password" <<'SQL'
SELECT format('CREATE ROLE anpr_gate LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anpr_gate') \gexec
ALTER ROLE anpr_gate PASSWORD :'app_password';
SELECT 'CREATE DATABASE anpr_gate OWNER anpr_gate'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'anpr_gate') \gexec
SQL
