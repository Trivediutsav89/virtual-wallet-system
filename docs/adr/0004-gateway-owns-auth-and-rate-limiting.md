# ADR-0004 — The gateway owns authentication enforcement and rate limiting

- **Status:** Accepted
- **Date:** 2026-11-18
- **Stage:** 1

## Context

With four services, token verification and throttling could be implemented
(a) in every service, (b) in a shared middleware library, or (c) once in the
gateway. Duplicating verification invites drift — one service eventually
forgets a check — while a shared library contradicts ADR-0001.

## Decision

**The gateway is the single enforcement point.**

1. **One public API surface.** Only `gateway:8000` (and later `frontend:3000`)
   is published. Service ports appear on the host only in the dev override.
2. **Verification-only JWT handling.** The gateway verifies and never signs. It
   owns no database and no bcrypt dependency.
3. **Identity injection with header hardening.** Inbound `X-User-Id`,
   `X-Token-Version`, and `X-Internal-*` headers are **stripped**, then
   re-injected from verified claims. The inbound `Authorization` header is
   dropped so downstream services cannot accidentally rely on re-verifying it.
4. **Rate limiting at the edge.** Redis fixed-window counters keyed by
   client IP and route class (10/min for `/auth/*`, 120/min otherwise).
   `429` includes `Retry-After` and `X-RateLimit-*` headers. On a Redis outage
   the limiter **fails open** and logs a warning: a cache blip must not become
   an outage.
5. **CORS only here.** Services do not configure CORS, so the policy cannot
   split across four codebases.
6. **Health split.** `/healthz` is process-local; `/readyz` reports dependency
   reachability.

## Consequences

**Positive**

- New services get authentication for free by being routed through the gateway.
- Security policy exists in exactly one place and is unit-testable in isolation.
- Services stay small: Profile and Wallet have no JWT, no bcrypt, no secrets.

**Negative / risks**

- The gateway is a single point of failure and a potential throughput bottleneck.
  Mitigation for later: run more than one replica behind a load balancer.
- **Downstream services trust `X-User-Id`.** This is only safe on a private
  network. If a service port is ever published, identity can be forged. The
  base Compose file therefore publishes only the gateway.
- Rate limiting per IP is imprecise behind NAT/proxies, and `X-Forwarded-For`
  is client-spoofable — hence `TRUST_FORWARDED_FOR=false` by default.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Verify in every service | Duplicated secrets and logic; drift guaranteed by Stage 3. |
| Token introspection call to Auth | Adds an Auth dependency and a network hop to every request. |
| Rate limit inside each service | Four Redis clients, four policies, and unauthenticated routes still need edge protection. |
| Per-service CORS | Policy fragmentation; preflight handled in four places. |

**Upgrade path:** mTLS or a signed internal token on service-to-service calls,
so `X-User-Id` is not the only proof of identity. Then the trust model survives
even if a service port is exposed.
