# Auth API — Stage 1

Base URL through the gateway: `http://localhost:8000/api/v1`

All request and response bodies are JSON (`application/json`). Timestamps are
ISO-8601 UTC. IDs are UUID strings.

## Token

| Property | Value |
|---|---|
| Algorithm | HS256 |
| Lifetime | 30 minutes (`ACCESS_TOKEN_TTL_MINUTES`) |
| Header | `Authorization: Bearer <token>` |
| Claims | `sub` (user UUID), `iss`, `iat`, `exp`, `jti`, `typ="access"`, `ver` |
| PII | none — do not expect email or roles in the payload |

```json
{
  "sub": "0e6c1c1e-1f3a-4d3d-9a2f-1c0d6b1b4a55",
  "iss": "virtual-wallet-auth",
  "iat": 1763500000,
  "exp": 1763501800,
  "jti": "7d1b3c1e-6f2a-4a9a-8c1f-2b4c5d6e7f80",
  "typ": "access",
  "ver": 0
}
```

There is no refresh token in Stage 1. When the access token expires the client
must call `/auth/login` again.

## Errors

All errors share one envelope:

```json
{ "detail": { "code": "INVALID_CREDENTIALS", "message": "Invalid email or password." } }
```

Validation failures keep FastAPI's 422 shape, but the submitted `input` is
never echoed back — not for `password`, and not for a body of the wrong shape.
Passwords must not end up in response bodies or client-side logs.

| HTTP | Code | Meaning |
|---|---|---|
| 400 | `BAD_REQUEST` | Malformed request |
| 401 | `AUTH_REQUIRED` | Missing/invalid identity header |
| 401 | `INVALID_TOKEN` | Token missing, malformed, expired, wrong issuer/type/signature |
| 401 | `INVALID_CREDENTIALS` | Wrong email **or** password (deliberately indistinguishable) |
| 403 | `ACCOUNT_INACTIVE` | Credentials valid, account disabled |
| 404 | `NOT_FOUND` | Unknown route |
| 409 | `EMAIL_ALREADY_REGISTERED` | Email already exists (case-insensitive) |
| 422 | — | Pydantic validation error |
| 429 | `RATE_LIMITED` | Too many requests; see `Retry-After` |
| 500 | `INTERNAL_ERROR` | Unhandled server error (details are logged, not returned) |
| 502 | `UPSTREAM_UNAVAILABLE` | Gateway cannot reach the service |
| 504 | `UPSTREAM_TIMEOUT` | Gateway timed out |

---

## `POST /auth/register`

Public. Creates a user. Does **not** issue a token and does **not** create a
profile or wallet — those are lazily created in later stages by the owning
service.

**Request**

```json
{ "email": "ada@example.com", "password": "correct-horse-battery" }
```

Password rules: at least 8 characters and at most 72 **bytes** (bcrypt's hard
limit). Characters are preserved exactly; whitespace is not trimmed. No
complexity rules.

**201 Created**

```json
{
  "id": "0e6c1c1e-1f3a-4d3d-9a2f-1c0d6b1b4a55",
  "email": "ada@example.com",
  "is_active": true,
  "is_verified": false,
  "created_at": "2026-11-18T10:15:30.123456Z"
}
```

| Status | Cause |
|---|---|
| 409 | Email already registered, including case variants (`Ada@…` vs `ada@…`) |
| 422 | Invalid email, password too short, or password over 72 bytes |

---

## `POST /auth/login`

Public.

**Request**

```json
{ "email": "ada@example.com", "password": "correct-horse-battery" }
```

**200 OK**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

| Status | Cause |
|---|---|
| 401 | Unknown email or wrong password — same body and comparable timing |
| 403 | Correct credentials but `is_active = false` |
| 429 | Login rate limit exceeded (default 10/min per IP) |

---

## `GET /auth/me`

Requires a valid access token **through the gateway**. The gateway injects
`X-User-Id`; the Auth service does not parse tokens itself.

```bash
curl http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer <token>"
```

**200 OK** — same shape as the registration response.

| Status | Cause |
|---|---|
| 401 | Gateway rejected the token, or the user no longer exists |
| 403 | Account deactivated after the token was issued |

---

## Rate limits (gateway)

| Scope | Default | Key |
|---|---|---|
| `/api/v1/auth/*` | 10/min | client IP |
| everything else | 120/min | client IP |

Headers on a limited response: `Retry-After`, `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, `X-RateLimit-Reset`. Limits are configured with
`RATE_LIMIT_AUTH` / `RATE_LIMIT_DEFAULT` using the `<count>/<s|min|hour>` form.
If Redis is unavailable the limiter fails open.
