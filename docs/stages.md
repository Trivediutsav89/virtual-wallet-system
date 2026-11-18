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

## Stage 2 — Wallet core

- Accounts and a double-entry ledger (`entries` referencing `accounts`),
  balances derived from entries, never stored as a mutable column.
- Idempotency keys on transfers.
- Lazy wallet creation on first authenticated access (idempotent on `user_id`).
- Concurrency safety: `SELECT ... FOR UPDATE` or serializable transactions.
- Tests for overdraft prevention, concurrency, and idempotency.

## Stage 3 — Profile

- Display name, avatar URL, preferences.
- Lazy profile creation keyed by `user_id`.
- `GET/PATCH /profile/me`.

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
