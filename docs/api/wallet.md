# Wallet API — Day 2

Base URL through the gateway: `http://localhost:8000/api/v1/wallet`

All endpoints require a verified JWT. The gateway injects the trusted
`X-User-Id`; the Wallet Service never accepts a user id from a request body.

Deposits and withdrawals are **simulated** local balance changes. No payment
gateway, transfers, ledger or transaction history exist in Day 2 (ADR-0005).

## `GET /me`

Returns the authenticated user's wallet, creating it lazily with `0.00 INR` if
it does not exist. Idempotent and concurrency-safe.

```json
{
  "id": "uuid",
  "user_id": "uuid",
  "balance": "0.00",
  "currency": "INR",
  "created_at": "2026-11-23T15:10:24.185484Z",
  "updated_at": "2026-11-23T15:10:24.185484Z"
}
```

`balance` is serialized as a fixed two-decimal string, never a JSON number.

## `POST /deposit`

```bash
curl -X POST http://localhost:8000/api/v1/wallet/deposit \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"amount":"500.00"}'
```

## `POST /withdraw`

```bash
curl -X POST http://localhost:8000/api/v1/wallet/withdraw \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"amount":"125.50"}'
```

Both return the updated wallet (same shape as `GET /me`).

## Amount rules

| Rule | Value |
|---|---|
| Type | decimal string or integer; **floats rejected** |
| Minimum | `> 0` |
| Scale | at most 2 decimal places |
| Per-operation maximum | `1000000.00` |
| Internal type | Python `Decimal`, PostgreSQL `NUMERIC(18,2)` |

Withdrawals use a conditional `UPDATE ... WHERE balance >= amount` inside a
transaction. A concurrent withdrawal cannot overspend; the database CHECK
constraint `balance >= 0` is the final guard.

## Errors

| HTTP | Code | Meaning |
|---|---|---|
| 401 | `AUTH_REQUIRED` | Missing/invalid gateway identity |
| 409 | `INSUFFICIENT_FUNDS` | Balance would become negative |
| 422 | — | Invalid amount or unknown field (`input` never echoed) |
| 500 | `INTERNAL_ERROR` | Unhandled service/database failure |
