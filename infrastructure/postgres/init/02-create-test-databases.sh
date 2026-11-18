#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Creates the isolated test databases used by pytest.
#
# Each is owned by the same service role as its production counterpart, so
# tests exercise the real grants (and cannot reach another service's data).
# Test data never touches the runtime databases.
# ---------------------------------------------------------------------------
set -euo pipefail

SUPERUSER="${POSTGRES_USER:-postgres}"

psql_super() {
  psql --username "$SUPERUSER" --dbname postgres --no-psqlrc --set ON_ERROR_STOP=1 "$@"
}

database_exists() {
  [ "$(psql_super -tAc "SELECT 1 FROM pg_database WHERE datname = '$1'")" = "1" ]
}

role_exists() {
  [ "$(psql_super -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$1'")" = "1" ]
}

create_test_database() {
  local db="$1" owner="$2"

  if ! role_exists "$owner"; then
    echo "FATAL: role $owner does not exist; run 01-create-databases.sh first" >&2
    exit 1
  fi

  if database_exists "$db"; then
    echo "  database $db already exists - skipping create"
  else
    echo "  creating test database $db owned by $owner"
    psql_super -q -c "CREATE DATABASE $db WITH OWNER $owner ENCODING 'UTF8' TEMPLATE template0;"
  fi

  psql_super -q -c "REVOKE ALL ON DATABASE $db FROM PUBLIC;"
  psql_super -q -c "GRANT CONNECT, TEMPORARY ON DATABASE $db TO $owner;"

  psql --username "$SUPERUSER" --dbname "$db" --no-psqlrc --set ON_ERROR_STOP=1 -q \
    -c "ALTER SCHEMA public OWNER TO $owner;" \
    -c "REVOKE ALL ON SCHEMA public FROM PUBLIC;" \
    -c "GRANT USAGE, CREATE ON SCHEMA public TO $owner;"
}

echo "==> virtual-wallet: creating test databases"

create_test_database auth_db_test auth_svc
create_test_database profile_db_test profile_svc
create_test_database wallet_db_test wallet_svc

echo "==> virtual-wallet: test databases ready"
