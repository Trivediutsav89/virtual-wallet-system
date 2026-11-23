# Profile API — Day 2

Base URL through the gateway: `http://localhost:8000/api/v1/profile`

All endpoints require a verified JWT. The gateway strips client identity
headers and injects the trusted `X-User-Id`; Profile never reads a user id from
a request body or query parameter (ADR-0004).

## `GET /me`

Returns the authenticated user's profile. If none exists, one is created
lazily and returned. The operation is idempotent and concurrency-safe.

```json
{
  "id": "uuid",
  "user_id": "uuid",
  "display_name": null,
  "phone_number": null,
  "created_at": "2026-11-23T15:10:24.085543Z",
  "updated_at": "2026-11-23T15:10:24.085543Z"
}
```

## `PATCH /me`

Partial update. Omitted fields are unchanged; an explicit `null` clears a
field. Unknown fields are rejected, so `user_id`, `id` and timestamps cannot be
changed. Strings are stripped and must be non-empty when provided.

| Field | Rule |
|---|---|
| `display_name` | string or null, at most 100 characters |
| `phone_number` | string or null, at most 32 characters |

```bash
curl -X PATCH http://localhost:8000/api/v1/profile/me \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"Ada Lovelace"}'
```

If no profile exists, the patch lazily creates one and then applies the update.

## Errors

Errors use `{"detail":{"code":"…","message":"…"}}`.

| HTTP | Code | Meaning |
|---|---|---|
| 401 | `AUTH_REQUIRED` | Missing/invalid gateway identity |
| 422 | — | Pydantic validation error (`input` is never echoed) |
| 500 | `INTERNAL_ERROR` | Unhandled service/database failure |

No email, password, role or other Auth data is stored in `profile_db`.
