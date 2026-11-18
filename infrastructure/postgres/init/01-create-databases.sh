#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Creates the three service roles and their databases with least privilege.
#
# Runs once, from the Postgres entrypoint, only when the data directory is
# empty. Safe to re-run manually (idempotent).
#
# Role model (ADR-0002):
#   auth_svc    -> auth_db     (owner)
#   profile_svc -> profile_db  (owner)
#   wallet_svc  -> wallet_db   (owner)
#
# No role can connect to another role's database: PUBLIC connect is revoked
# and CONNECT is granted only to the owning role.
# ---------------------------------------------------------------------------
set -euo pipefail

SUPERUSER="${POSTGRES_USER:-postgres}"

psql_super() {
  psql --username "$SUPERUSER" --dbname postgres --no-psqlrc --set ON_ERROR_STOP=1 "$@"
}

require_alnum() {
  local name="$1" value="${2:-}"
  if [ -z "$value" ]; then
    echo "FATAL: $name is not set" >&2
    exit 1
  fi
  # Keeps values safe for interpolation into SQL and DSNs. Hex is recommended.
  case "$value" in
    *[!A-Za-z0-9]*) echo "FATAL: $name must be alphanumeric (use hex)" >&2; exit 1 ;;
  esac
}

role_exists() {
  [ "$(psql_super -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$1'")" = "1" ]
}

database_exists() {
  [ "$(psql_super -tAc "SELECT 1 FROM pg_database WHERE datname = '$1'")" = "1" ]
}

create_role() {
  local role="$1" password="$2"
  if role_exists "$role"; then
    echo "  role $role already exists - rotating password"
    psql_super -q -c "ALTER ROLE $role WITH LOGIN PASSWORD '$password' NOSUPERUSER NOCREATEDB NOCREATEROLE;"
  else
    echo "  creating role $role"
    psql_super -q -c "CREATE ROLE $role WITH LOGIN PASSWORD '$password' NOSUPERUSER NOCREATEDB NOCREATEROLE;"
  fi
}

create_database() {
  local db="$1" owner="$2"
  if database_exists "$db"; then
    echo "  database $db already exists - skipping create"
  else
    echo "  creating database $db owned by $owner"
    psql_super -q -c "CREATE DATABASE $db WITH OWNER $owner ENCODING 'UTF8' TEMPLATE template0;"
  fi

  # Least privilege: nobody but the owner may even connect.
  psql_super -q -c "REVOKE ALL ON DATABASE $db FROM PUBLIC;"
  psql_super -q -c "GRANT CONNECT, TEMPORARY ON DATABASE $db TO $owner;"

  # The service owns the only schema it can reach.
  psql --username "$SUPERUSER" --dbname "$db" --no-psqlrc --set ON_ERROR_STOP=1 -q \
    -c "ALTER SCHEMA public OWNER TO $owner;" \
    -c "REVOKE ALL ON SCHEMA public FROM PUBLIC;" \
    -c "GRANT USAGE, CREATE ON SCHEMA public TO $owner;"
}

echo "==> virtual-wallet: creating service roles and databases"

require_alnum AUTH_DB_PASSWORD "${AUTH_DB_PASSWORD:-}"
require_alnum PROFILE_DB_PASSWORD "${PROFILE_DB_PASSWORD:-}"
require_alnum WALLET_DB_PASSWORD "${WALLET_DB_PASSWORD:-}"

create_role auth_svc "${AUTH_DB_PASSWORD}"
create_role profile_svc "${PROFILE_DB_PASSWORD}"
create_role wallet_svc "${WALLET_DB_PASSWORD}"

create_database auth_db auth_svc
create_database profile_db profile_svc
create_database wallet_db wallet_svc

echo "==> virtual-wallet: databases ready"
psql_super -c "\l auth_db profile_db wallet_db"
