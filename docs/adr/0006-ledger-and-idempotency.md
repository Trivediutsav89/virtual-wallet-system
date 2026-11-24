# ADR-0006 — Immutable double-entry ledger, idempotency and transaction engine

- **Status:** Accepted
- **Date:** 2026-11-24
- **Stage:** 3
- **Supersedes in scope:** ADR-0005 (which remains valid for Day 2 history)

## Context

Day 2 stored `wallets.balance` as the source of truth for simulated INR deposits
and withdrawals. ADR-0005 deferred the immutable ledger, transfers, history and
idempotency to Day 3. Day 3 must now introduce genuine double-entry accounting
without losing or resetting existing balances.

## Decision

1. **Ledger is the source of truth.** `wallets.balance` is retained as a cached
   projection for fast reads and backward-compatible responses.
2. **Genuine double-entry accounting.** Every posted transaction has at least
   two immutable entries; `SUM(DEBIT) = SUM(CREDIT) = ledger_transactions.amount`
   per transaction and currency.
3. **Account model.** Each wallet has exactly one `USER_WALLET` ledger account.
   Singleton system accounts: `SYSTEM_CASH_CLEARING` (asset) and
   `SYSTEM_OPENING_EQUITY` (contra-equity, used only for opening backfill).
4. **API surface stays under `/api/v1/wallet/`** (no Gateway production change).
5. **`Idempotency-Key` is required** on all money-changing POST endpoints.
6. **Deterministic business failures** (insufficient funds, unknown recipient,
   balance limit) are stored and replayed. Unexpected DB/internal/transient
   errors are rolled back and never cached.
7. **Transfers require an existing destination wallet.** No wallet is created
   for an arbitrary UUID. No cross-database access; a stronger Auth user lookup
   is deferred to a future ADR.
8. **Reconciliation is report-only** and must never modify ledger history.
9. **Existing wallet balances are preserved exactly** by a dedicated opening
   backfill in phase 3.2. The operation cap does not apply to opening entries.

## Mandatory corrections incorporated

### A. Entry insertion race protection

`BEFORE INSERT ON ledger_entries` executes `validate_new_ledger_entry()`, which
performs `SELECT status, currency ... FROM ledger_transactions WHERE id = NEW.transaction_id
FOR UPDATE`. This locks the parent transaction row, verifies it is still
`PENDING`, and checks currency against both transaction and account. Posting
(`UPDATE ledger_transactions SET status='POSTED'`) takes the same row lock, so
entry insertion and posting are serialised. A PostgreSQL concurrency test is
required.

### B. Transaction amount invariant

A `DEFERRABLE INITIALLY DEFERRED` constraint trigger
`ctrg_assert_posted_transaction_invariants` fires at COMMIT when a transaction
becomes `POSTED` and asserts:

- at least two entries;
- `SUM(DEBIT) = SUM(CREDIT)`;
- `SUM(DEBIT) = SUM(CREDIT) = ledger_transactions.amount`;
- every entry currency matches the transaction and its account currency.

Reconciliation must cover this invariant as well.

### C. Idempotency reconciliation

In the single-transaction design, `IN_PROGRESS` is never committed: the
idempotency row is inserted in the same transaction as the financial operation
and is updated to `COMPLETED` or `FAILED` before COMMIT. Concurrent duplicate
requests wait on the unique `(scope, idempotency_key)` index and replay the
committed response. Any committed `IN_PROGRESS` record is therefore an anomaly
and must be flagged by reconciliation.

### D. Migration backfill protection

Phase 3.2 backfill must use a database-enforced uniqueness mechanism (a
dedicated `ledger_migrations` marker or unique constraint on opening entries),
not merely a pre-check, to prevent accidental duplicate backfill. Each wallet's
opening entry amount must equal its existing balance exactly.

## Consequences

- Strong DB-enforced invariants; application bugs cannot post invalid ledgers.
- Ledger triggers add write overhead but keep correctness centralised.
- `wallet_svc` owns the schema, so `REVOKE` is insufficient for immutability;
  triggers are the primary enforcement mechanism.
- Reconciliation is mandatory to detect projection drift and anomalous records.
- No new service role or shared runtime library is introduced (ADR-0001/0002).
