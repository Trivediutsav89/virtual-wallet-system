# TODO — Virtual Wallet System

Live implementation checklist. Updated after every completed milestone.
Status legend: `[x]` done, `[~]` written but not runtime-verified, `[ ]` pending.

---

## Stage 1 — Foundation, Auth Service, API Gateway

### M1 · Repository hygiene
- [x] `.gitignore`, `.gitattributes` (LF for shell scripts), `.editorconfig`
- [x] Root `.dockerignore` + per-service `.dockerignore`
- [x] `.env.example` and generated local `.env`
- [x] `README.md` quickstart, ports, troubleshooting
- [x] `TODO.md` (this file)

### M2 · Architecture Decision Records
- [x] ADR-0001 monorepo and service layout
- [x] ADR-0002 single Postgres instance, three databases
- [x] ADR-0003 HS256 shared secret for Stage 1
- [x] ADR-0004 gateway owns auth enforcement and rate limiting
- [x] `docs/architecture.md`, `docs/stages.md`

### M3 · Docker Compose
- [~] `docker-compose.yml` (base topology) + `docker-compose.override.yml` (dev)
- [~] Healthchecks, `depends_on` ordering, one-shot `auth-migrate`
- [~] Internal services unpublished outside the dev override
- [x] Static validation: 7 services, anchors resolved, healthchecks present,
      ordering correct, no internal ports in base, all env vars declared
- [x] Dev override publishes Postgres/Redis on 5433/6380 for host tools and the
      integration suite; the base file keeps both network-internal
- [ ] **`docker compose config` / live boot** — blocked: Docker is not installed

### M4 · PostgreSQL initialization
- [~] `01-create-databases.sh` — 3 DBs + 3 least-privilege roles
- [~] `02-create-test-databases.sh` — `*_db_test` + same grants
- [x] `bash -n` syntax check passes; LF line endings confirmed
- [ ] **Verified with a live Postgres** — blocked: no Docker/Postgres available

### M5 · Redis configuration
- [~] `infrastructure/redis/redis.conf` — 128 MB, LRU, persistence off
- [x] Config contains real redis directives (not prose); referenced by Compose
- [ ] **Verified with a live Redis** — blocked: no Docker/Redis available

### M6 · API Gateway skeleton
- [x] Config, JSON logging, request-id + access-log middleware
- [x] Verify-only JWT module (signature, `exp` with leeway, `iss`, `typ`)
- [x] Header hardening (`X-User-*` / `X-Internal-*` stripped incoming)
- [x] Route table + generic httpx proxy, timeout/error mapping
- [x] Redis fixed-window rate limiter, fail-open on outage
- [x] CORS, `/healthz`, `/readyz`
- [x] **Verified**: `ruff` + `mypy` clean, **73 tests passing** without Docker
      (fakeredis + respx). Two real bugs caught by tests: upstream path did not
      strip the `/wallet` prefix, and client `X-Request-Id` was forwarded twice.

### M7 · Service skeletons
- [x] Auth, Profile, Wallet share the same layout, settings, logging, DB wiring
- [x] Profile and Wallet are health-only (`/healthz`, `/readyz`) with their own DB
- [x] Profile and Wallet each have an empty baseline Alembic migration
- [x] **Verified**: Profile 4/4 tests, Wallet 4/4 tests, `ruff` + `mypy` clean;
      `alembic upgrade head --sql` renders for both (offline check)

### M8 · Auth Service implementation
- [~] `users` model + functional unique index on `lower(email)`
      (SQL rendered offline; index not yet created on a live database)
- [~] Alembic `0001_create_users` (offline `--sql` output reviewed:
      `CREATE TABLE users ...` + `CREATE UNIQUE INDEX ux_users_email_lower`)
- [x] bcrypt hashing (cost 12, threadpool, 72-byte guard, no passlib)
- [x] `POST /auth/register` (201, no token), `POST /auth/login`, `GET /auth/me`
- [x] JWT: `sub, iss, iat, exp, jti, typ, ver`; TTL 30 min; no PII
- [x] Uniform error envelope; `IntegrityError` → 409; no password (or raw body)
      leakage in 422s — pydantic's `input` is dropped for every error
- [x] Registration never calls Profile or Wallet (asserted by an AST import test)
- [x] Alembic uses a DB-only settings model, so `auth-migrate` does not need
      `JWT_SECRET` (the original `env.py` would have crashed the migrate job)

### M9 · Tests and verification
- [x] Auth: registration, duplicate/case-variant email, validation, login,
  enumeration resistance, inactive account, `/me`
- [x] Auth: token claim/TTL assertions, signature and issuer checks
- [x] Auth: **89 unit tests passing**, 15 DB-backed tests skipped (no Postgres)
- [x] Auth: `ruff` + `mypy app tests` clean
- [x] Gateway: proxy, 401 matrix, header spoofing, 429, Redis-down fail-open
  (**73 tests passing**)
- [x] Unit suites runnable without infrastructure (fakeredis, respx, fake session)
- [x] Integration suite written (15 tests) with production-schema migrations,
      savepoint isolation, and explicit skip when no database is reachable
- [ ] **Full DB-backed suite executed** — blocked: no Postgres/Docker
- [ ] **End-to-end curl flow through the gateway** — blocked: no Docker

### Environment blocker (not an architecture change)
Docker Desktop, WSL, Podman, PostgreSQL and Redis are all absent on this
machine. Consequently these remain unverified and must be run on a Docker-capable
host before Stage 1 is declared done:

1. `docker compose config` and `docker compose up --build` (7 services).
2. `alembic upgrade head` against a live Postgres (auth/profile/wallet).
3. `pytest tests/integration` for Auth (15 tests, currently skipped).
4. The README curl flow: register → login → `/auth/me` through the gateway.

Every one of these is written and statically checked; none has executed.

### Known gaps carried forward
- [ ] `ghcr.io/astral-sh/uv:latest` is unpinned in all four Dockerfiles. Pin to a
      digest before deployment (could not verify tag availability offline).
- [ ] Gateway test fixtures are untyped (`mypy` override relaxes `tests.*` only).
- [ ] Frontend, wallet/profile data models, refresh tokens: out of Stage 1 scope.

---

## Stage 2+ (not started, recorded for scope control)
- [ ] Wallet Service: accounts, double-entry ledger, balances, transfers
- [ ] Profile Service: display name, preferences
- [ ] Lazy wallet/profile creation keyed by `user_id` (no distributed write on signup)
- [ ] Refresh tokens / token revocation via `token_version`
- [ ] Next.js login/register + dashboard
- [ ] RS256 + JWKS; internal service auth; per-service Postgres instances
