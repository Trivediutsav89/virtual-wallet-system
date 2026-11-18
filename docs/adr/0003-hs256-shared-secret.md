# ADR-0003 — HS256 access tokens with a shared secret in Stage 1

- **Status:** Accepted (revisit before production)
- **Date:** 2026-11-18
- **Stage:** 1

## Context

Auth issues tokens; the gateway verifies them. Symmetric HMAC (HS256) needs one
shared secret. Asymmetric signing (RS256) needs key management: a private key in
Auth, a public key or JWKS endpoint for the gateway, and rotation procedure.

## Decision

- **HS256**, single `JWT_SECRET` (≥32 bytes, hex) shared by exactly two
  services: `auth` (signs) and `gateway` (verifies).
- Claims: `sub`, `iss`, `iat`, `exp`, `jti`, `typ`, `ver`.
- `exp = iat + 30 minutes` (`ACCESS_TOKEN_TTL_MINUTES=30`).
- Verification requires `exp`, `iat`, `sub`, `iss`, `typ`; rejects `typ != "access"`;
  allows 30 seconds of clock leeway.
- Downstream services (Profile, Wallet) **do not verify tokens at all** — they
  trust gateway-injected `X-User-Id` headers.
- Secrets are environment-injected; `.env` is git-ignored and images contain no
  secrets.

## Consequences

**Positive**

- Zero key-management work for the MVP; verification is a few microseconds.
- Revocation-free statelessness; horizontal scaling needs no shared session store.
- Only two services hold the secret, keeping the blast radius small.

**Negative / risks**

- **Anyone with the secret can mint tokens.** A leak in either service is total
  compromise. Treat `JWT_SECRET` as the highest-value secret in the system.
- Rotation requires restarting both services simultaneously (no `kid` header,
  no dual-key window).
- Cannot verify tokens without the signing secret, which blocks third-party
  verification and per-service verification later.

## Alternatives considered

| Alternative | Why deferred |
|---|---|
| RS256 + JWKS | Correct long-term choice. Adds key generation/storage, a JWKS endpoint, and rotation logic — roughly half a day, no MVP benefit. |
| Opaque tokens + introspection | Requires a DB/Redis lookup per request, and couples every request to Auth availability. |
| `python-jose` / `authlib` | Broader dependency surface than PyJWT for the same three functions. |

## Migration path (Stage 5)

1. Auth generates an RSA or Ed25519 keypair; private key stays in Auth.
2. Auth publishes `/.well-known/jwks.json`; tokens gain `kid` and `alg=RS256`.
3. Gateway switches to public-key verification and caches JWKS.
4. `JWT_SECRET` is removed from both services.
