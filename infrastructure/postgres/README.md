# PostgreSQL init and ownership

The `init/` directory is mounted at `/docker-entrypoint-initdb.d` and runs
**only when the data directory is empty** — i.e. on the first `docker compose up`
or after `docker compose down -v`.

| Script | Purpose |
|---|---|
| `01-create-databases.sh` | Three roles, three databases, grants, schema ownership |
| `02-create-test-databases.sh` | `auth_db_test`, `profile_db_test`, `wallet_db_test` |

## Resulting model

| Database | Owner role | Reachable by |
|---|---|---|
| `auth_db` | `auth_svc` | Auth Service only |
| `profile_db` | `profile_svc` | Profile Service only |
| `wallet_db` | `wallet_svc` | Wallet Service only |
| `auth_db_test` | `auth_svc` | Pytest only |
| `profile_db_test` | `profile_svc` | Pytest only |
| `wallet_db_test` | `wallet_svc` | Pytest only |

Each script:

1. Creates the role with `NOSUPERUSER NOCREATEDB NOCREATEROLE`.
2. Creates the database with that role as owner.
3. `REVOKE ALL ON DATABASE … FROM PUBLIC`, then grants `CONNECT, TEMPORARY`
   to the owner.
4. Makes the role the owner of schema `public` and revokes `PUBLIC` access.

Consequences: a service cannot connect to another service's database, and no
role can create objects in a schema it does not own.

## Verification

```bash
# as the superuser: list databases and owners
docker compose exec postgres psql -U postgres -c "\l"

# exists? should succeed
docker compose exec postgres psql -U auth_svc -d auth_db -c "select current_user, current_database();"

# isolation? should fail with "permission denied for database"
docker compose exec postgres psql -U auth_svc -d wallet_db -c "select 1;"

# confirm roles are not superusers
docker compose exec postgres psql -U postgres -c \
  "select rolname, rolsuper, rolcreatedb, rolcreaterole from pg_roles where rolname like '%_svc';"
```

## Changing credentials or databases

Init scripts do not re-run against an existing volume. After editing `.env` or
these scripts:

```bash
docker compose down -v
docker compose up --build
```

This destroys all data. That is acceptable for Stage 1; a real deployment needs
a versioned migration/rotation process instead.

## Constraints

Passwords are restricted to alphanumeric characters (hex recommended) by the
init scripts, because they are interpolated into both SQL and `asyncpg` DSNs.
