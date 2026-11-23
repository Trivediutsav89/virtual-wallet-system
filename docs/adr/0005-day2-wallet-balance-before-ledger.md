# ADR-0005 — Stored wallet balance for Day 2, immutable ledger in Day 3

- **Status:** Accepted
- **Date:** 2026-11-23
- **Stage:** 2 (Day 2 implementation)

## Context

`docs/stages.md` originally described the wallet core as a double-entry ledger
with balances derived from immutable entries and no mutable balance column.
The Day 2 delivery brief explicitly excludes transfers, double-entry postings,
a ledger and transaction-history APIs. It requires a working simulated deposit
and withdrawal flow with a `NUMERIC(18,2)` balance.

These two statements conflict. Building the ledger now would expand Day 2
scope substantially and contradict the brief; storing a balance while claiming
the ledger design is unchanged would also be dishonest.

## Decision

- Day 2 stores `wallets.balance NUMERIC(18,2)` as the source of truth for the
  simulated wallet.
- The database enforces `balance >= 0` with a CHECK constraint and a fixed
  `currency = 'INR'` policy.
- Deposits and withdrawals are single conditional `UPDATE` statements executed
  in a transaction. No intermediate `transactions`, `entries` or `accounts`
  table is introduced in Day 2.
- No operation history is exposed. Deposits and withdrawals are **simulated**
  local balance changes, not payment processing.
- Day 3 will introduce the immutable ledger. The stored balance will then
  either be replaced by a derived balance or retained explicitly as a cached
  projection reconciled from ledger entries. That choice will be recorded in a
  follow-up ADR before the ledger migration is written.

## Consequences

**Positive**

- Day 2 is small, testable and matches the brief.
- No throwaway transaction table needs to be deleted or rewritten in Day 3.
- Concurrency safety is achieved with PostgreSQL row locking plus a CHECK
  constraint, without a distributed transaction.

**Negative / risks**

- The stored balance is not an audit trail. Day 2 cannot answer “why” a balance
  changed, only “what” it is now.
- Day 3 must include a deliberate data migration/reconciliation step. This is
  documented rather than hidden.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Double-entry ledger now | Contradicts the Day 2 brief; adds accounts, entries, idempotency and transfer logic. |
| Temporary `wallet_operations` table | Would need redesign or removal in Day 3; no Day 2 API consumes it. |
| Floating-point balance | Binary floating point cannot represent decimal currency exactly. |
