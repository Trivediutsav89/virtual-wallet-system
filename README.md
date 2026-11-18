# Virtual Wallet System

A five-day backend engineering MVP: four FastAPI microservices, one database per
service, Redis-backed rate limiting, and a Next.js frontend (later stages).

> **Stage 1 status:** infrastructure + Auth Service + API Gateway are implemented.
> Profile and Wallet are health-only skeletons with their own database and
> baseline migration. No wallet or profile logic, no refresh tokens, no UI yet.

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
least-privilege roles. `auth-migrate` applies migrations, then `auth` starts.

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

Migrations run automatically via the `auth-migrate` service. To run them
manually:

```bash
docker compose run --rm auth-migrate
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

Every service has its own suite; see
`backend/services/auth/tests/README.md` for what is covered and how the
DB-backed tests isolate themselves.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `password authentication failed for user "auth_svc"` | Init scripts only run on an empty data directory. `docker compose down -v` and start again. |
| Port 5433/6380 already in use | Edit `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` in `.env`. |
| `entrypoint.sh: not found` / `^M` errors | Line endings were converted to CRLF. `.gitattributes` enforces LF; re-checkout the file. |
| Gateway returns 502 | Upstream service is not healthy: `docker compose ps`. |
| `alembic` cannot connect | Wrong DSN or the migrate service ran before Postgres was healthy. |
