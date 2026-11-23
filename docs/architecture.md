# Architecture — Virtual Wallet System (Stage 1)

## 1. Topology

```
                            ┌─────────────────────────┐
   browser (later stages)   │  frontend  :3000        │
        ───────────────────▶│  Next.js App Router     │
                            └───────────┬─────────────┘
                                        │ http://gateway:8000/api/v1
                            ┌───────────▼─────────────┐
                            │  gateway   :8000        │
                            │  · routing              │
                            │  · JWT verification     │
                            │  · rate limiting        │
                            │  · request ids, CORS    │
                            └──┬────────┬────────┬────┘
                               │        │        │
                  ┌────────────▼──┐ ┌───▼───────┐ ┌▼──────────────┐
                  │ auth   :8000  │ │ profile   │ │ wallet        │
                  │ (container)   │ │  :8000    │ │  :8000        │
                  └───────┬───────┘ └─────┬─────┘ └──────┬────────┘
                          │               │              │
                          ▼               ▼              ▼
                     ┌────────────────────────────────────────┐
                     │ postgres:5432                          │
                     │  auth_db · profile_db · wallet_db      │
                     │  (one instance, separate roles)        │
                     └────────────────────────────────────────┘
                          ▲
                     ┌────┴─────┐
                     │ redis    │  gateway only (rate limits)
                     └──────────┘
```

Container ports are uniformly `8000`. Host ports differ (8001/8002/8003) and are
published only in the dev override.

## 2. Request flow (authenticated call)

```
1. Client                    GET /api/v1/wallet/me
                             Authorization: Bearer <jwt>
2. gateway  RequestID        assign/propagate X-Request-Id
3. gateway  RateLimit        INCR rl:default:<ip>:<window>   → 429 on excess
4. gateway  RouteMatch       /wallet → WALLET_SERVICE_URL
5. gateway  JWT guard        verify signature, exp (30 s leeway), iss, typ=access
                             strip inbound X-User-*, X-Internal-*
                             inject X-User-Id, X-Token-Version, X-Request-Id
                             drop Authorization (downstream does not need it)
6. gateway  Proxy            httpx → http://wallet:8000/me
7. wallet                    trusts X-User-Id (see trust boundary below)
8. gateway                   pass through status, body, safe headers
```

Public routes bypass steps 5–6 identity injection: `POST /auth/register`,
`POST /auth/login`, `/healthz`, `/readyz`, and `/docs` outside production.

## 3. Trust boundaries

| Boundary | Rule |
|---|---|
| Internet → gateway | Untrusted. All headers validated; rate limited. |
| Gateway → services | Trusted network. Only the gateway is published; services never accept `X-User-Id` from an untrusted peer. |
| Service → its database | One role, one database, `CONNECT` revoked from `PUBLIC`. |
| Service → another service | None in Stage 1. Registration performs no fan-out. |

**Known limitation:** downstream services trust `X-User-Id` without re-verifying
a token. This is safe only while service ports are not reachable from untrusted
clients. Replace with mTLS or signed internal tokens before exposing anything
beyond a private network (see ADR-0004).

## 4. Database ownership

| Database | Role | Service | Notes |
|---|---|---|---|
| `auth_db` | `auth_svc` | Auth | `users` |
| `profile_db` | `profile_svc` | Profile | `profiles` (Day 2) |
| `wallet_db` | `wallet_svc` | Wallet | `wallets` (Day 2, stored INR balance) |
| `*_test` | same role | Pytest | never touched by running apps |

No cross-database foreign keys. `user_id` is an opaque UUID owned by Auth.
Each service owns its Alembic history; there is no central migrations directory.

## 5. Identity

- Access token only, HS256, 30-minute TTL.
- Claims: `sub`, `iss`, `iat`, `exp`, `jti`, `typ`, `ver`.
- No PII (email/name) in the token — JWTs are signed, not encrypted.
- No refresh token and no sessions table in Stage 1. `users.token_version`
  exists so global revocation can be added without a schema migration.
- Logout is client-side token discard; exposure is bounded by the TTL.

## 6. Configuration

Per-container environment variables, validated by `pydantic-settings` at
startup (fail fast). Compose maps variables **explicitly per service** rather
than using a blanket `env_file`, so each container only sees what it needs.
The only value shared between two services is `JWT_SECRET`.

## 7. Failure behaviour

| Failure | Behaviour |
|---|---|
| Redis unavailable | Rate limiter **fails open** and logs a warning; auth still works. |
| Upstream unreachable | Gateway returns `502 UPSTREAM_UNAVAILABLE`. |
| Upstream timeout | Gateway returns `504 UPSTREAM_TIMEOUT`. |
| Database down | Service `/readyz` returns 503; traffic errors are 500s with no internals leaked. |
| Duplicate email race | Unique index raises `IntegrityError` → `409 EMAIL_ALREADY_REGISTERED`. |
| Concurrent first profile/wallet access | `INSERT ... ON CONFLICT DO NOTHING RETURNING` makes lazy creation idempotent. |
| Concurrent withdrawals | Conditional `UPDATE ... WHERE balance >= amount` plus CHECK constraint prevents overdraft; loser gets `409 INSUFFICIENT_FUNDS`. |

## 8. Deliberate non-goals for Stage 1

Refresh tokens, email verification, password reset, roles/permissions, wallet
and profile features, UI screens, distributed tracing, Kafka, Kubernetes,
service discovery, cross-service sagas.

## 9. Day 2 additions

| External route | Upstream path | Service | Behaviour |
|---|---|---|---|
| `GET /api/v1/profile/me` | `/me` | Profile | Lazy create + return |
| `PATCH /api/v1/profile/me` | `/me` | Profile | Partial update; explicit null clears |
| `GET /api/v1/wallet/me` | `/me` | Wallet | Lazy create with `0.00 INR` |
| `POST /api/v1/wallet/deposit` | `/deposit` | Wallet | Atomic simulated deposit |
| `POST /api/v1/wallet/withdraw` | `/withdraw` | Wallet | Atomic withdrawal, overdraft rejected |

Gateway routing already supported these prefixes; only tests were added.
Profile and Wallet trust only the gateway-injected `X-User-Id`. The mutable
balance and its Day 3 ledger migration path are recorded in ADR-0005.
