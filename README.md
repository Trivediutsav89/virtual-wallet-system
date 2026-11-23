# Virtual Wallet System

A five-day backend engineering MVP: four FastAPI microservices, one database per
service, Redis-backed rate limiting, and a Next.js frontend (later stages).

> **Status:** Stage 1 (infrastructure, Auth, Gateway) is complete and verified.
> Day 2 adds functional Profile and Wallet services: lazy creation, profile
> updates, simulated INR deposits/withdrawals, atomic balance updates, and
> their own migrations and tests. No transfers, ledger, refresh tokens or UI
> yet — see ADR-0005 for the Day 2/Day 3 balance decision.

## Architecture at a glance

```
browser ──▶ frontend :3000 (later)
                │
                ▼
        gateway :8000  ── JWT verification, rate limiting, routing
         │        │        │
         ▼        ▼        ▼
      auth     profile   wallet
      :8000     :8000     :8000        (container ports)
         │        │        │
         └────────┴────────┘
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
   postgres:5432        redis:6379
   (3 databases)        (rate limits)
```

Only the gateway is a public API surface. Service ports are published on the
host in the dev override purely for debugging.

## Documentation

| Document | Contents |
|---|---|
| `docs/architecture.md` | Topology, request flow, trust boundaries |
| `docs/api/auth.md` | Auth endpoints, claims, error codes |
| `docs/api/profile.md` | Profile endpoints and validation rules |
| `docs/api/wallet.md` | Wallet endpoints, money rules, simulated operations |
| `docs/stages.md` | Stage-by-stage roadmap |
| `docs/adr/` | Architecture decision records |
| `TODO.md` | Live implementation checklist |

## Prerequisites

- Docker Desktop (Compose v2) — Linux containers
- `uv` (optional, for running services/tests outside Docker)
- Python 3.12+ (only needed for local, non-Docker work)

## Quickstart

```bash
cp .env.example .env      # then replace the change-me values
docker compose up --build
```

On first boot the Postgres init scripts create the three databases and three
least-privilege roles. One-shot `auth-migrate`, `profile-migrate` and
`wallet-migrate` jobs apply each service's migrations before the services start.

Smoke test through the gateway:

```bash
# register
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"ada@example.com","password":"correct-horse-battery"}'

# login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"ada@example.com","password":"correct-horse-battery"}'

# authenticated call
curl http://localhost:8000/api/v1/auth/me \
  -H "Authorization: Bearer <access_token>"

# profile (created lazily on first access)
curl http://localhost:8000/api/v1/profile/me \
  -H "Authorization: Bearer <access_token>"

curl -X PATCH http://localhost:8000/api/v1/profile/me \
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
  -d '{"display_name":"Ada Lovelace"}'

# wallet (created lazily with 0.00 INR)
curl http://localhost:8000/api/v1/wallet/me \
  -H "Authorization: Bearer <access_token>"

curl -X POST http://localhost:8000/api/v1/wallet/deposit \
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
  -d '{"amount":"500.00"}'

curl -X POST http://localhost:8000/api/v1/wallet/withdraw \
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
  -d '{"amount":"125.50"}'
```

Windows users: the examples above are POSIX shell; use PowerShell equivalents
(`Invoke-RestMethod`) or Git Bash. There is no `Makefile` at this stage.

## Ports

| Component | Host | Container |
|---|---|---|
| Gateway | 8000 | 8000 |
| Auth (dev only) | 8001 | 8000 |
| Profile (dev only) | 8002 | 8000 |
| Wallet (dev only) | 8003 | 8000 |
| Postgres | 5433 | 5432 |
| Redis | 6380 | 6379 |

## Common commands

```bash
docker compose up --build -d            # start everything (dev override merged)
docker compose logs -f gateway auth     # tail logs
docker compose -f docker-compose.yml up -d   # production-shaped: only the gateway is published
docker compose down                     # stop (keeps data)
docker compose down -v                  # stop and wipe databases
```

Migrations run automatically via the three `*-migrate` one-shot services. To
run them manually:

```bash
docker compose run --rm auth-migrate
docker compose run --rm profile-migrate
docker compose run --rm wallet-migrate
```

## Local (non-Docker) development

Each backend service is an independent `uv` project:

```bash
cd backend/services/auth
uv sync
AUTH_DATABASE_URL="postgresql+asyncpg://auth_svc:<pw>@localhost:5433/auth_db" \
JWT_SECRET="<same secret as .env>" \
uv run uvicorn app.main:app --reload --port 8001
```

Tests:

```bash
cd backend/services/auth
uv run pytest                 # unit tests run anywhere; DB tests skip if unavailable

# with a reachable test database:
AUTH_TEST_DATABASE_URL="postgresql+asyncpg://auth_svc:<pw>@localhost:5433/auth_db_test" \
  uv run pytest
```

Every service has its own suite. The DB-backed suites use separate
`*_db_test` databases via `AUTH_TEST_DATABASE_URL`,
`PROFILE_TEST_DATABASE_URL` and `WALLET_TEST_DATABASE_URL`; they skip cleanly
when the DSN is absent. See `backend/services/auth/tests/README.md` for the
isolation strategy shared by Profile and Wallet.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `password authentication failed for user "auth_svc"` | Init scripts only run on an empty data directory. `docker compose down -v` and start again. |
| Port 5433/6380 already in use | Edit `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` in `.env`. |
| `entrypoint.sh: not found` / `^M` errors | Line endings were converted to CRLF. `.gitattributes` enforces LF; re-checkout the file. |
| Gateway returns 502 | Upstream service is not healthy: `docker compose ps`. |
| `alembic` cannot connect | Wrong DSN or the migrate service ran before Postgres was healthy. |
