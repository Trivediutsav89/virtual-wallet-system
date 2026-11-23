# Stage Roadmap

Scope is controlled deliberately: each stage must be independently demonstrable
before the next begins.

## Stage 1 — Foundation + Auth + Gateway (current)

**Ships:** monorepo, Compose topology, three databases with least-privilege
roles, Redis rate limiting, Auth Service (register/login/me), API Gateway
(routing, JWT verification, rate limiting), health-only Profile and Wallet
skeletons with baseline migrations, test suites and docs.

**Does not ship:** wallet logic, profile logic, refresh tokens, UI.

**Done when:** a fresh clone plus `docker compose up` yields a working
register → login → authenticated request flow, with tests covering the full
auth matrix and three databases whose ownership is enforced by grants.

## Stage 2 / Day 2 — Wallet core + Profile (implemented)

**Ships:** lazy `profiles` and `wallets` keyed by the trusted `user_id`,
`GET/PATCH /profile/me`, `GET /wallet/me`, simulated `POST /deposit` and
`POST /withdraw`, a stored `NUMERIC(18,2)` INR balance guarded by a CHECK
constraint and conditional updates, one-shot migrate jobs, and DB-backed tests
for lazy creation, ownership, atomicity, rollback and concurrency.

**Does not ship:** transfers, double-entry ledger, transaction history,
idempotency keys, avatar/preferences, FX or multi-currency.

The original plan called for a double-entry ledger with derived balances.
ADR-0005 records the deliberate Day 2 deviation: the stored balance is the MVP
source of truth, and Day 3 introduces the immutable ledger plus an explicit
reconciliation/migration step.

## Stage 3 — Ledger and richer Profile

- Immutable double-entry ledger and the migration/reconciliation from the Day 2
  stored balance (ADR-0005).
- Idempotency keys on transfers.
- Avatar URL and preferences on profiles.
- `SELECT ... FOR UPDATE` or serializable transactions where the ledger needs it.

## Stage 4 — Frontend

- Next.js App Router, Tailwind, zod.
- Login/register, dashboard, transfer form.
- Token storage decision recorded as an ADR (httpOnly cookie + CSRF vs header).

## Stage 5 — Hardening

- Refresh tokens and revocation via `token_version`.
- RS256 + JWKS; internal service authentication.
- Per-service Postgres instances.
- Observability: metrics, tracing, structured log shipping.
- CI pipeline: lint, type-check, tests, image build, migration check.
