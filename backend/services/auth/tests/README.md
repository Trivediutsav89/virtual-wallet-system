# Auth Service tests

```bash
cd backend/services/auth
uv run pytest
```

## Two groups

| Group | Path | Needs PostgreSQL | Notes |
|---|---|---|---|
| Unit | `tests/unit` | no | ASGI transport, fake session, fakes for the service layer |
| Integration | `tests/integration` | yes | real schema, real SQL, real bcrypt |

Unit tests set safe dummy environment values in `tests/conftest.py`
(`BCRYPT_COST=4` for speed, a throwaway `JWT_SECRET`). Nothing in that file is
valid for a deployment.

Integration tests **skip** with an explicit message unless
`AUTH_TEST_DATABASE_URL` is set and reachable:

```bash
export AUTH_TEST_DATABASE_URL="postgresql+asyncpg://auth_svc:<AUTH_DB_PASSWORD>@localhost:5433/auth_db_test"
uv run pytest tests/integration -v
```

```powershell
$env:AUTH_TEST_DATABASE_URL = "postgresql+asyncpg://auth_svc:<AUTH_DB_PASSWORD>@localhost:5433/auth_db_test"
uv run pytest tests/integration -v
```

The variable is deliberately separate from `AUTH_DATABASE_URL`: a typo can
never point the suite at the runtime database. The `*_db_test` databases are
created by `infrastructure/postgres/init/02-create-test-databases.sh`.

## Isolation

`tests/integration/conftest.py`:

1. Probes the DSN with a 5 s timeout and skips (does not fail) if no database
   is available.
2. Runs `alembic upgrade head` once per session, in a subprocess whose
   `AUTH_DATABASE_URL` is the test DSN. A migration failure is a **hard
   failure**, not a skip.
3. Opens one outer transaction per test; the SQLAlchemy session uses
   `join_transaction_mode="create_savepoint"`, so a service-level `commit()`
   only releases a savepoint. The outer transaction is always rolled back.

## What the suite pins

- Registration: 201 without a token, bcrypt hash (never plaintext), trimmed
  email, case-insensitive duplicate detection mapped to 409.
- Login: 30-minute token with exactly `sub, iss, iat, exp, jti, typ, ver`,
  `last_login_at` recorded, identical failure for unknown email and wrong
  password, 403 only after the password check for a disabled account.
- `/auth/me`: identity comes from `X-User-Id` only; a bearer token presented
  directly to the service is ignored; deleted identity → 401, disabled → 403.
- Schemas: 72-byte bcrypt limit measured in bytes, unknown fields rejected,
  passwords never echoed in 422 responses.
- Queries: the functional unique index on `lower(email)` exists, and the
  `users` table has no session/refresh-token columns.

## Still unverified without Docker

The integration suite has never run on this machine: Docker, WSL, Podman and a
local PostgreSQL are all unavailable. It is written to fail loudly (not skip)
on a broken migration once a database is reachable.
