"""Phase 3.1 ledger invariant tests against the real test database.

All tests use ``wallet_db_test`` via the ``test_dsn`` fixture. Tests that need
COMMIT-time deferred constraint checks do so with an explicit transaction that
is always rolled back, except where a real commit is required (concurrency,
real COMMIT rejection and migration downgrade). Those tests clean up in
``finally`` blocks.

Currency validation note
------------------------
``ledger_accounts.currency`` and ``ledger_entries.currency`` both have
``CHECK (currency = 'INR')``, and the BEFORE INSERT trigger rejects an entry
whose currency differs from its transaction or account. Consequently the
deferred currency checks in ``assert_posted_transaction_invariants()`` are
defensive and are not independently reachable through valid inserts: the only
reachable currency rejection path is the BEFORE INSERT trigger, which is
covered by ``test_entry_currency_mismatch_is_rejected``. The constraints are
intentionally NOT weakened to manufacture an unreachable deferred test.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

SERVICE_ROOT = Path(__file__).resolve().parents[2]
DEFERRED_CONSTRAINT = "ctrg_assert_posted_transaction_invariants"
SYSTEM_TYPES = ("SYSTEM_CASH_CLEARING", "SYSTEM_OPENING_EQUITY")
EXPECTED_TABLES = {
    "ledger_accounts",
    "ledger_transactions",
    "ledger_entries",
    "idempotency_records",
    "ledger_opening_backfills",
}
EXPECTED_FUNCTIONS = {
    "reject_ledger_entry_mutation",
    "validate_new_ledger_entry",
    "enforce_ledger_transaction_state",
    "reject_ledger_transaction_delete",
    "assert_posted_transaction_invariants",
    "protect_ledger_account_identity",
    "ledger_backfill_entry_delete_authorized",
    "ledger_backfill_transaction_delete_authorized",
}
EXPECTED_TRIGGERS = {
    "trg_ledger_entries_immutable|ledger_entries",
    "trg_ledger_entries_posted_guard|ledger_entries",
    "trg_ledger_transactions_state|ledger_transactions",
    "trg_ledger_transactions_no_delete|ledger_transactions",
    "ctrg_assert_posted_transaction_invariants|ledger_transactions",
    "trg_ledger_accounts_identity|ledger_accounts",
}
EXPECTED_INDEXES = {
    "ux_ledger_accounts_wallet",
    "ux_ledger_accounts_system_type",
    "ix_ledger_transactions_kind_created",
    "ix_ledger_transactions_status_created",
    "ix_ledger_entries_transaction",
    "ix_ledger_entries_account_created",
    "ix_idempotency_status_created",
    "ux_ledger_opening_backfills_transaction",
    "ix_ledger_opening_backfills_account",
}
EXPECTED_CHECK_FRAGMENTS = {
    "wallets|balance <= 9999999999999999.99",
    "ledger_accounts|'USER_WALLET'::text",
    "ledger_accounts|(currency)::text = 'INR'::text",
    "ledger_transactions|amount > (0)::numeric",
    "ledger_transactions|'PENDING'::character varying",
    "ledger_transactions|posted_at IS NOT NULL",
    "ledger_transactions|(currency)::text = 'INR'::text",
    "ledger_transactions|amount <= 1000000.00",
    "ledger_entries|amount > (0)::numeric",
    "ledger_entries|'DEBIT'::character varying",
    "ledger_entries|(currency)::text = 'INR'::text",
    "idempotency_records|'IN_PROGRESS'::character varying",
    "ledger_opening_backfills|opening_balance >= (0)::numeric",
    "ledger_opening_backfills|transaction_id IS NOT NULL",
}
EXPECTED_FKS = {
    "ledger_accounts|wallets|r",
    "ledger_entries|ledger_transactions|r",
    "ledger_entries|ledger_accounts|r",
    "idempotency_records|ledger_transactions|r",
}


@asynccontextmanager
async def _tx(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Yield a connection in a transaction that is always rolled back."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            if transaction.is_active:
                await transaction.rollback()


async def _seed_wallet(connection: AsyncConnection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    wallet_id = uuid.UUID(
        str(
            await connection.scalar(
                text(
                    "INSERT INTO wallets (user_id, balance, currency) "
                    "VALUES (:uid, 0.00, 'INR') RETURNING id"
                ),
                {"uid": user_id},
            )
        )
    )
    await connection.execute(
        text(
            "INSERT INTO ledger_accounts (account_type, wallet_id, currency, name) "
            "VALUES ('USER_WALLET', :wid, 'INR', 'test wallet')"
        ),
        {"wid": wallet_id},
    )
    account_id = uuid.UUID(
        str(
            await connection.scalar(
                text("SELECT id FROM ledger_accounts WHERE wallet_id = :wid"),
                {"wid": wallet_id},
            )
        )
    )
    cash_id = uuid.UUID(
        str(
            await connection.scalar(
                text("SELECT id FROM ledger_accounts WHERE account_type = 'SYSTEM_CASH_CLEARING'")
            )
        )
    )
    return wallet_id, account_id, cash_id


async def _create_transaction(
    connection: AsyncConnection,
    *,
    kind: str = "DEPOSIT",
    amount: str = "100.00",
) -> uuid.UUID:
    return uuid.UUID(
        str(
            await connection.scalar(
                text(
                    "INSERT INTO ledger_transactions "
                    "(kind, amount, currency, status, metadata) "
                    "VALUES (:kind, :amount, 'INR', 'PENDING', '{}'::jsonb) RETURNING id"
                ),
                {"kind": kind, "amount": amount},
            )
        )
    )


async def _add_entry(
    connection: AsyncConnection,
    transaction_id: uuid.UUID,
    account_id: uuid.UUID,
    direction: str,
    amount: str,
    *,
    currency: str = "INR",
) -> uuid.UUID:
    return uuid.UUID(
        str(
            await connection.scalar(
                text(
                    "INSERT INTO ledger_entries "
                    "(transaction_id, account_id, direction, amount, currency) "
                    "VALUES (:tid, :aid, :direction, :amount, :currency) RETURNING id"
                ),
                {
                    "tid": transaction_id,
                    "aid": account_id,
                    "direction": direction,
                    "amount": amount,
                    "currency": currency,
                },
            )
        )
    )


async def _post(connection: AsyncConnection, transaction_id: uuid.UUID) -> None:
    await connection.execute(
        text(
            "UPDATE ledger_transactions "
            "SET status = 'POSTED', posted_at = now() WHERE id = :tid"
        ),
        {"tid": transaction_id},
    )


async def _force_deferred_check(connection: AsyncConnection) -> None:
    await connection.execute(text(f"SET CONSTRAINTS {DEFERRED_CONSTRAINT} IMMEDIATE"))


async def _backend_pid(connection: AsyncConnection) -> int:
    return int(await connection.scalar(text("SELECT pg_backend_pid()")))


async def _wait_for_lock_wait(
    engine: AsyncEngine,
    *,
    blocker_pid: int,
    waiter_pid: int,
    wait_timeout: float = 5.0,
) -> None:
    """Wait until ``waiter_pid`` is blocked on a row lock held by ``blocker_pid``."""
    statement = text(
        """
        SELECT count(*)
        FROM pg_locks AS waiter
        JOIN pg_locks AS blocker
          ON blocker.locktype = 'transactionid'
         AND blocker.granted
         AND blocker.transactionid::text = waiter.transactionid::text
        WHERE waiter.pid = :waiter_pid
          AND NOT waiter.granted
          AND waiter.locktype = 'transactionid'
          AND blocker.pid = :blocker_pid
        """
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_timeout
    while loop.time() < deadline:
        async with engine.connect() as connection:
            count = await connection.scalar(
                statement, {"waiter_pid": waiter_pid, "blocker_pid": blocker_pid}
            )
        if count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"lock wait by pid {waiter_pid} on pid {blocker_pid} was not observed"
    )


async def _setup_committed_wallet(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with engine.begin() as connection:
        return await _seed_wallet(connection)


async def _setup_pending_transaction(
    engine: AsyncEngine,
    *,
    amount: str = "100.00",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Commit a wallet plus a balanced PENDING transaction and return its ids."""
    async with engine.begin() as connection:
        wallet_id, account_id, cash_id = await _seed_wallet(connection)
        transaction_id = await _create_transaction(connection, amount=amount)
        debit_id = await _add_entry(connection, transaction_id, cash_id, "DEBIT", amount)
        credit_id = await _add_entry(connection, transaction_id, account_id, "CREDIT", amount)
    return wallet_id, account_id, cash_id, transaction_id, debit_id, credit_id


async def _setup_posted_transaction(
    engine: AsyncEngine,
    *,
    amount: str = "200.00",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with engine.begin() as connection:
        wallet_id, account_id, cash_id = await _seed_wallet(connection)
        transaction_id = await _create_transaction(connection, amount=amount)
        await _add_entry(connection, transaction_id, cash_id, "DEBIT", amount)
        await _add_entry(connection, transaction_id, account_id, "CREDIT", amount)
        await _post(connection, transaction_id)
        await _force_deferred_check(connection)
    return wallet_id, account_id, cash_id, transaction_id


async def _cleanup_wallet(
    engine: AsyncEngine,
    wallet_id: uuid.UUID,
    transaction_ids: list[uuid.UUID] | None = None,
) -> None:
    async with engine.begin() as connection:
        for transaction_id in transaction_ids or []:
            await connection.execute(
                text("DELETE FROM idempotency_records WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            await connection.execute(
                text("DELETE FROM ledger_entries WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            await connection.execute(
                text("DELETE FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
        await connection.execute(
            text("DELETE FROM ledger_accounts WHERE wallet_id = :wid"), {"wid": wallet_id}
        )
        await connection.execute(
            text("DELETE FROM wallets WHERE id = :wid"), {"wid": wallet_id}
        )


async def _catalog_snapshot(engine: AsyncEngine) -> dict[str, set[str]]:
    async with engine.connect() as connection:
        tables = {
            str(row[0])
            for row in (
                await connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ).all()
        }
        functions = {
            str(row[0])
            for row in (
                await connection.execute(
                    text(
                        "SELECT p.proname FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'public' AND p.proname = ANY(:names)"
                    ),
                    {"names": list(EXPECTED_FUNCTIONS)},
                )
            ).all()
        }
        triggers = {
            f"{row[0]}|{row[1]}"
            for row in (
                await connection.execute(
                    text(
                        "SELECT t.tgname, c.relname FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal AND t.tgname = ANY(:names)"
                    ),
                    {"names": [name.split("|")[0] for name in EXPECTED_TRIGGERS]},
                )
            ).all()
        }
        indexes = {
            str(row[0])
            for row in (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'public' AND indexname = ANY(:names)"
                    ),
                    {"names": list(EXPECTED_INDEXES)},
                )
            ).all()
        }
        checks = {
            f"{row[0]}|{row[1]}"
            for row in (
                await connection.execute(
                    text(
                        "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) "
                        "FROM pg_constraint WHERE contype = 'c'"
                    )
                )
            ).all()
        }
        foreign_keys = {
            f"{row[0]}|{row[1]}|{row[2].decode() if isinstance(row[2], bytes) else row[2]}"
            for row in (
                await connection.execute(
                    text(
                        "SELECT conrelid::regclass::text, confrelid::regclass::text, "
                        "confdeltype FROM pg_constraint WHERE contype = 'f'"
                    )
                )
            ).all()
        }
    return {
        "tables": tables,
        "functions": functions,
        "triggers": triggers,
        "indexes": indexes,
        "checks": checks,
        "foreign_keys": foreign_keys,
    }


async def _truncate_and_reseed(engine: AsyncEngine) -> None:
    """Global cleanup for tests that intentionally commit POSTED ledger rows.

    Posted entries and headers cannot be deleted through normal DML because the
    immutability triggers being tested reject those operations. This helper is
    therefore used only where targeted cleanup is impossible. The integration
    conftest serialises the whole suite with a PostgreSQL advisory lock so this
    table-level TRUNCATE cannot race another worker.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE ledger_entries, ledger_transactions, "
                "idempotency_records, ledger_accounts, wallets RESTART IDENTITY CASCADE"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO ledger_accounts (account_type, currency, name) VALUES "
                "('SYSTEM_CASH_CLEARING', 'INR', 'System cash clearing'), "
                "('SYSTEM_OPENING_EQUITY', 'INR', 'System opening equity') "
                "ON CONFLICT (account_type) WHERE account_type <> 'USER_WALLET' DO NOTHING"
            )
        )


def _alembic(test_dsn: str, command: str, revision: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "WALLET_DATABASE_URL": test_dsn}
    return subprocess.run(  # noqa: S603 - arguments are fixed by this test module
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision],
        cwd=SERVICE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_valid_balanced_transaction_can_be_posted(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "100.00")
            await _post(connection, transaction_id)
            await _force_deferred_check(connection)
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            assert status == "POSTED"
    finally:
        await engine.dispose()


async def test_unbalanced_transaction_cannot_be_posted(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "90.00")
            await _post(connection, transaction_id)
            with pytest.raises(Exception, match="not balanced"):
                await _force_deferred_check(connection)
    finally:
        await engine.dispose()


async def test_transaction_with_fewer_than_two_entries_cannot_be_posted(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, _, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _post(connection, transaction_id)
            with pytest.raises(Exception, match="at least two entries"):
                await _force_deferred_check(connection)
    finally:
        await engine.dispose()


async def test_transaction_header_amount_mismatch_cannot_be_posted(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection, amount="100.00")
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "50.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "50.00")
            await _post(connection, transaction_id)
            with pytest.raises(Exception, match="amount does not match entries"):
                await _force_deferred_check(connection)
    finally:
        await engine.dispose()


async def test_commit_rejects_transaction_with_fewer_than_two_entries(
    test_dsn: str, migrated: None
) -> None:
    """A real COMMIT must fire the deferred trigger and roll everything back."""
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    wallet_id, _account_id, cash_id = await _setup_committed_wallet(engine)
    transaction_id: uuid.UUID | None = None
    try:
        with pytest.raises(Exception, match="at least two entries"):
            async with engine.begin() as connection:
                transaction_id = await _create_transaction(connection, amount="100.00")
                await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
                await _post(connection, transaction_id)
        assert transaction_id is not None
        async with engine.connect() as connection:
            headers = await connection.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            entries = await connection.scalar(
                text("SELECT count(*) FROM ledger_entries WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            assert headers == 0
            assert entries == 0
    finally:
        await _cleanup_wallet(engine, wallet_id, [transaction_id] if transaction_id else None)
        await engine.dispose()


async def test_commit_rejects_unbalanced_transaction(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    wallet_id, account_id, cash_id = await _setup_committed_wallet(engine)
    transaction_id: uuid.UUID | None = None
    try:
        with pytest.raises(Exception, match="not balanced"):
            async with engine.begin() as connection:
                transaction_id = await _create_transaction(connection, amount="100.00")
                await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
                await _add_entry(connection, transaction_id, account_id, "CREDIT", "90.00")
                await _post(connection, transaction_id)
        assert transaction_id is not None
        async with engine.connect() as connection:
            headers = await connection.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            entries = await connection.scalar(
                text("SELECT count(*) FROM ledger_entries WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            assert headers == 0
            assert entries == 0
    finally:
        await _cleanup_wallet(engine, wallet_id, [transaction_id] if transaction_id else None)
        await engine.dispose()


async def test_commit_rejects_balanced_entries_not_matching_header(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    wallet_id, account_id, cash_id = await _setup_committed_wallet(engine)
    transaction_id: uuid.UUID | None = None
    try:
        with pytest.raises(Exception, match="amount does not match entries"):
            async with engine.begin() as connection:
                transaction_id = await _create_transaction(connection, amount="100.00")
                await _add_entry(connection, transaction_id, cash_id, "DEBIT", "50.00")
                await _add_entry(connection, transaction_id, account_id, "CREDIT", "50.00")
                await _post(connection, transaction_id)
        assert transaction_id is not None
        async with engine.connect() as connection:
            headers = await connection.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            entries = await connection.scalar(
                text("SELECT count(*) FROM ledger_entries WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            assert headers == 0
            assert entries == 0
    finally:
        await _cleanup_wallet(engine, wallet_id, [transaction_id] if transaction_id else None)
        await engine.dispose()


async def test_entry_currency_mismatch_is_rejected(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, _, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            with pytest.raises(Exception, match="transaction currency"):
                await _add_entry(
                    connection, transaction_id, cash_id, "DEBIT", "100.00", currency="USD"
                )
    finally:
        await engine.dispose()


async def test_update_of_posted_entry_is_rejected(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            entry_id = await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "100.00")
            await _post(connection, transaction_id)
            await _force_deferred_check(connection)
            with pytest.raises(Exception, match="immutable"):
                await connection.execute(
                    text("UPDATE ledger_entries SET amount = 200.00 WHERE id = :eid"),
                    {"eid": entry_id},
                )
    finally:
        await engine.dispose()


async def test_delete_of_posted_entry_is_rejected(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            entry_id = await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "100.00")
            await _post(connection, transaction_id)
            await _force_deferred_check(connection)
            with pytest.raises(Exception, match="immutable"):
                await connection.execute(
                    text("DELETE FROM ledger_entries WHERE id = :eid"), {"eid": entry_id}
                )
    finally:
        await engine.dispose()


async def test_insert_into_posted_transaction_is_rejected(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "100.00")
            await _post(connection, transaction_id)
            await _force_deferred_check(connection)
            with pytest.raises(Exception, match="posted transaction"):
                await _add_entry(connection, transaction_id, cash_id, "DEBIT", "1.00")
    finally:
        await engine.dispose()


async def test_update_of_posted_transaction_header_is_rejected(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "100.00")
            await _post(connection, transaction_id)
            await _force_deferred_check(connection)
            with pytest.raises(Exception, match="headers are immutable"):
                await connection.execute(
                    text("UPDATE ledger_transactions SET amount = 500.00 WHERE id = :tid"),
                    {"tid": transaction_id},
                )
    finally:
        await engine.dispose()


async def test_delete_of_posted_transaction_header_is_rejected(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "100.00")
            await _post(connection, transaction_id)
            await _force_deferred_check(connection)
            with pytest.raises(Exception, match="cannot be deleted"):
                await connection.execute(
                    text("DELETE FROM ledger_transactions WHERE id = :tid"),
                    {"tid": transaction_id},
                )
    finally:
        await engine.dispose()


async def test_invalid_status_transitions_are_rejected(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            with pytest.raises(Exception, match="created as PENDING"):
                await connection.execute(
                    text(
                        "INSERT INTO ledger_transactions "
                        "(kind, amount, currency, status, posted_at, metadata) "
                        "VALUES ('DEPOSIT', 100.00, 'INR', 'POSTED', now(), '{}'::jsonb)"
                    )
                )
        async with _tx(engine) as connection:
            _, account_id, cash_id = await _seed_wallet(connection)
            transaction_id = await _create_transaction(connection)
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "100.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "100.00")
            await _post(connection, transaction_id)
            await _force_deferred_check(connection)
            with pytest.raises(Exception, match="immutable"):
                await connection.execute(
                    text(
                        "UPDATE ledger_transactions SET status = 'PENDING' WHERE id = :tid"
                    ),
                    {"tid": transaction_id},
                )
    finally:
        await engine.dispose()


async def test_moving_entry_into_posted_transaction_is_rejected(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _wallet_id, _account_id, cash_id, posted_id = await _setup_posted_transaction(engine)
        async with engine.begin() as connection:
            pending_id = await _create_transaction(connection, amount="100.00")
            entry_id = await _add_entry(connection, pending_id, cash_id, "DEBIT", "100.00")

        async with _tx(engine) as connection:
            with pytest.raises(Exception, match="transaction cannot be changed"):
                await connection.execute(
                    text("UPDATE ledger_entries SET transaction_id = :tid WHERE id = :eid"),
                    {"tid": posted_id, "eid": entry_id},
                )
    finally:
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_moving_entry_between_pending_transactions_is_rejected(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id, _account_id, cash_id = await _setup_committed_wallet(engine)
        async with engine.begin() as connection:
            first_id = await _create_transaction(connection, amount="100.00")
            entry_id = await _add_entry(connection, first_id, cash_id, "DEBIT", "100.00")
            second_id = await _create_transaction(connection, amount="100.00")

        async with _tx(engine) as connection:
            with pytest.raises(Exception, match="transaction cannot be changed"):
                await connection.execute(
                    text("UPDATE ledger_entries SET transaction_id = :tid WHERE id = :eid"),
                    {"tid": second_id, "eid": entry_id},
                )
    finally:
        await _cleanup_wallet(engine, wallet_id, [first_id, second_id])
        await engine.dispose()


async def test_ledger_account_identity_fields_are_immutable(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            wallet_a, account_a, _cash_a = await _seed_wallet(connection)
            wallet_b, _account_b, _cash_b = await _seed_wallet(connection)

        attempts = (
            (
                text("UPDATE ledger_accounts SET wallet_id = :wid WHERE id = :aid"),
                {"wid": wallet_b, "aid": account_a},
            ),
            (
                text(
                    "UPDATE ledger_accounts "
                    "SET account_type = 'SYSTEM_CASH_CLEARING' WHERE id = :aid"
                ),
                {"aid": account_a},
            ),
            (
                text("UPDATE ledger_accounts SET currency = 'USD' WHERE id = :aid"),
                {"aid": account_a},
            ),
        )
        for statement, parameters in attempts:
            async with _tx(engine) as connection:
                with pytest.raises(Exception, match="identity fields"):
                    await connection.execute(statement, parameters)
    finally:
        await _cleanup_wallet(engine, wallet_a)
        await _cleanup_wallet(engine, wallet_b)
        await engine.dispose()


async def test_system_ledger_account_cannot_be_deleted(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            system_id = await connection.scalar(
                text(
                    "SELECT id FROM ledger_accounts "
                    "WHERE account_type = 'SYSTEM_CASH_CLEARING'"
                )
            )
            with pytest.raises(Exception, match="system ledger accounts cannot be deleted"):
                await connection.execute(
                    text("DELETE FROM ledger_accounts WHERE id = :aid"), {"aid": system_id}
                )
    finally:
        await engine.dispose()


async def test_entry_update_serializes_against_posting(test_dsn: str, migrated: None) -> None:
    """Entry UPDATE locks the parent; posting waits and then fails the invariant."""
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        (
            wallet_id,
            _account_id,
            _cash_id,
            transaction_id,
            debit_id,
            _credit_id,
        ) = await _setup_pending_transaction(engine, amount="100.00")

        a_pid_ready = asyncio.Event()
        a_updated = asyncio.Event()
        b_pid_ready = asyncio.Event()
        b_waiting = asyncio.Event()
        b_done = asyncio.Event()
        holder: dict[str, int] = {}

        async def update_entry() -> None:
            async with engine.begin() as connection:
                holder["a_pid"] = await _backend_pid(connection)
                a_pid_ready.set()
                # No manual lock: the BEFORE UPDATE trigger must lock the parent.
                await connection.execute(
                    text("UPDATE ledger_entries SET amount = 150.00 WHERE id = :eid"),
                    {"eid": debit_id},
                )
                a_updated.set()
                await asyncio.wait_for(b_waiting.wait(), timeout=10)

        async def post_transaction() -> None:
            await asyncio.wait_for(a_updated.wait(), timeout=10)
            with pytest.raises(Exception, match="not balanced"):
                async with engine.begin() as connection:
                    holder["b_pid"] = await _backend_pid(connection)
                    b_pid_ready.set()
                    await _post(connection, transaction_id)
                    await _force_deferred_check(connection)
            b_done.set()

        async def observe_lock_wait() -> None:
            await asyncio.wait_for(a_pid_ready.wait(), timeout=10)
            await asyncio.wait_for(b_pid_ready.wait(), timeout=10)
            await _wait_for_lock_wait(
                engine, blocker_pid=holder["a_pid"], waiter_pid=holder["b_pid"]
            )
            b_waiting.set()

        results = await asyncio.gather(
            update_entry(), post_transaction(), observe_lock_wait(), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await asyncio.wait_for(b_done.wait(), timeout=10)

        async with engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            amounts = (
                await connection.execute(
                    text(
                        "SELECT amount FROM ledger_entries "
                        "WHERE transaction_id = :tid ORDER BY amount"
                    ),
                    {"tid": transaction_id},
                )
            ).scalars().all()
            assert status == "PENDING"
            assert amounts == [Decimal("100.00"), Decimal("150.00")]
    finally:
        await _cleanup_wallet(engine, wallet_id, [transaction_id])
        await engine.dispose()


async def test_posting_serializes_against_entry_update(test_dsn: str, migrated: None) -> None:
    """Posting wins the parent lock; the later entry UPDATE sees POSTED and fails."""
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        (
            _wallet_id,
            _account_id,
            _cash_id,
            transaction_id,
            debit_id,
            _credit_id,
        ) = await _setup_pending_transaction(engine, amount="100.00")

        a_posted = asyncio.Event()
        b_pid_ready = asyncio.Event()
        b_waiting = asyncio.Event()
        b_done = asyncio.Event()
        holder: dict[str, int] = {}

        async def post_transaction() -> None:
            async with engine.begin() as connection:
                holder["a_pid"] = await _backend_pid(connection)
                await _post(connection, transaction_id)
                await _force_deferred_check(connection)
                a_posted.set()
                await asyncio.wait_for(b_waiting.wait(), timeout=10)

        async def update_entry() -> None:
            await asyncio.wait_for(a_posted.wait(), timeout=10)
            with pytest.raises(Exception, match="immutable"):
                async with engine.begin() as connection:
                    holder["b_pid"] = await _backend_pid(connection)
                    b_pid_ready.set()
                    await connection.execute(
                        text("UPDATE ledger_entries SET amount = 150.00 WHERE id = :eid"),
                        {"eid": debit_id},
                    )
            b_done.set()

        async def observe_lock_wait() -> None:
            await asyncio.wait_for(b_pid_ready.wait(), timeout=10)
            await _wait_for_lock_wait(
                engine, blocker_pid=holder["a_pid"], waiter_pid=holder["b_pid"]
            )
            b_waiting.set()

        results = await asyncio.gather(
            post_transaction(), update_entry(), observe_lock_wait(), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await asyncio.wait_for(b_done.wait(), timeout=10)

        async with engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            amounts = (
                await connection.execute(
                    text(
                        "SELECT amount FROM ledger_entries "
                        "WHERE transaction_id = :tid ORDER BY amount"
                    ),
                    {"tid": transaction_id},
                )
            ).scalars().all()
            assert status == "POSTED"
            assert amounts == [Decimal("100.00"), Decimal("100.00")]
    finally:
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_entry_delete_serializes_against_posting(test_dsn: str, migrated: None) -> None:
    """Entry DELETE locks the parent; posting waits and then fails the invariant."""
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        (
            wallet_id,
            _account_id,
            _cash_id,
            transaction_id,
            debit_id,
            _credit_id,
        ) = await _setup_pending_transaction(engine, amount="100.00")

        a_pid_ready = asyncio.Event()
        a_deleted = asyncio.Event()
        b_pid_ready = asyncio.Event()
        b_waiting = asyncio.Event()
        b_done = asyncio.Event()
        holder: dict[str, int] = {}

        async def delete_entry() -> None:
            async with engine.begin() as connection:
                holder["a_pid"] = await _backend_pid(connection)
                a_pid_ready.set()
                # No manual lock: the BEFORE DELETE trigger must lock the parent.
                await connection.execute(
                    text("DELETE FROM ledger_entries WHERE id = :eid"), {"eid": debit_id}
                )
                a_deleted.set()
                await asyncio.wait_for(b_waiting.wait(), timeout=10)

        async def post_transaction() -> None:
            await asyncio.wait_for(a_deleted.wait(), timeout=10)
            with pytest.raises(Exception, match="at least two entries"):
                async with engine.begin() as connection:
                    holder["b_pid"] = await _backend_pid(connection)
                    b_pid_ready.set()
                    await _post(connection, transaction_id)
                    await _force_deferred_check(connection)
            b_done.set()

        async def observe_lock_wait() -> None:
            await asyncio.wait_for(a_pid_ready.wait(), timeout=10)
            await asyncio.wait_for(b_pid_ready.wait(), timeout=10)
            await _wait_for_lock_wait(
                engine, blocker_pid=holder["a_pid"], waiter_pid=holder["b_pid"]
            )
            b_waiting.set()

        results = await asyncio.gather(
            delete_entry(), post_transaction(), observe_lock_wait(), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await asyncio.wait_for(b_done.wait(), timeout=10)

        async with engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            count = await connection.scalar(
                text("SELECT count(*) FROM ledger_entries WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            assert status == "PENDING"
            assert count == 1
    finally:
        await _cleanup_wallet(engine, wallet_id, [transaction_id])
        await engine.dispose()


async def test_posting_serializes_against_entry_delete(test_dsn: str, migrated: None) -> None:
    """Posting wins the parent lock; the later entry DELETE sees POSTED and fails."""
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        (
            _wallet_id,
            _account_id,
            _cash_id,
            transaction_id,
            debit_id,
            _credit_id,
        ) = await _setup_pending_transaction(engine, amount="100.00")

        a_posted = asyncio.Event()
        b_pid_ready = asyncio.Event()
        b_waiting = asyncio.Event()
        b_done = asyncio.Event()
        holder: dict[str, int] = {}

        async def post_transaction() -> None:
            async with engine.begin() as connection:
                holder["a_pid"] = await _backend_pid(connection)
                await _post(connection, transaction_id)
                await _force_deferred_check(connection)
                a_posted.set()
                await asyncio.wait_for(b_waiting.wait(), timeout=10)

        async def delete_entry() -> None:
            await asyncio.wait_for(a_posted.wait(), timeout=10)
            with pytest.raises(Exception, match="immutable"):
                async with engine.begin() as connection:
                    holder["b_pid"] = await _backend_pid(connection)
                    b_pid_ready.set()
                    await connection.execute(
                        text("DELETE FROM ledger_entries WHERE id = :eid"), {"eid": debit_id}
                    )
            b_done.set()

        async def observe_lock_wait() -> None:
            await asyncio.wait_for(b_pid_ready.wait(), timeout=10)
            await _wait_for_lock_wait(
                engine, blocker_pid=holder["a_pid"], waiter_pid=holder["b_pid"]
            )
            b_waiting.set()

        results = await asyncio.gather(
            post_transaction(), delete_entry(), observe_lock_wait(), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await asyncio.wait_for(b_done.wait(), timeout=10)

        async with engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            count = await connection.scalar(
                text("SELECT count(*) FROM ledger_entries WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            assert status == "POSTED"
            assert count == 2
    finally:
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_entry_insertion_trigger_lock_wins_over_posting(
    test_dsn: str, migrated: None
) -> None:
    """The BEFORE INSERT trigger's FOR UPDATE is the only lock in transaction A."""
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    async with engine.begin() as connection:
        _wallet_id, account_id, cash_id = await _seed_wallet(connection)
        transaction_id = await _create_transaction(connection, amount="200.00")

    a_inserted = asyncio.Event()
    a_pid_ready = asyncio.Event()
    b_pid_ready = asyncio.Event()
    b_waiting = asyncio.Event()
    b_done = asyncio.Event()
    holder: dict[str, int] = {}

    async def insert_entries() -> None:
        async with engine.begin() as connection:
            holder["a_pid"] = await _backend_pid(connection)
            a_pid_ready.set()
            # No manual SELECT ... FOR UPDATE: the trigger must acquire the lock.
            await _add_entry(connection, transaction_id, cash_id, "DEBIT", "200.00")
            await _add_entry(connection, transaction_id, account_id, "CREDIT", "200.00")
            a_inserted.set()
            await asyncio.wait_for(b_waiting.wait(), timeout=10)
            # Exiting the context commits and releases the trigger's lock.

    async def post_transaction() -> None:
        await asyncio.wait_for(a_inserted.wait(), timeout=10)
        async with engine.begin() as connection:
            holder["b_pid"] = await _backend_pid(connection)
            b_pid_ready.set()
            await _post(connection, transaction_id)
            await _force_deferred_check(connection)
        b_done.set()

    async def observe_lock_wait() -> None:
        await asyncio.wait_for(a_pid_ready.wait(), timeout=10)
        await asyncio.wait_for(b_pid_ready.wait(), timeout=10)
        await _wait_for_lock_wait(
            engine,
            blocker_pid=holder["a_pid"],
            waiter_pid=holder["b_pid"],
        )
        b_waiting.set()

    try:
        results = await asyncio.gather(
            insert_entries(),
            post_transaction(),
            observe_lock_wait(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await asyncio.wait_for(b_done.wait(), timeout=10)

        async with engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            count = await connection.scalar(
                text("SELECT count(*) FROM ledger_entries WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            assert status == "POSTED"
            assert count == 2
    finally:
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_posting_lock_wins_over_entry_insertion(
    test_dsn: str, migrated: None
) -> None:
    """Posting holds the parent lock; a concurrent insert waits and then fails."""
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    async with engine.begin() as connection:
        _wallet_id, account_id, cash_id = await _seed_wallet(connection)
        transaction_id = await _create_transaction(connection, amount="200.00")
        await _add_entry(connection, transaction_id, cash_id, "DEBIT", "200.00")
        await _add_entry(connection, transaction_id, account_id, "CREDIT", "200.00")

    a_posted = asyncio.Event()
    b_pid_ready = asyncio.Event()
    b_waiting = asyncio.Event()
    b_failed = asyncio.Event()
    holder: dict[str, int] = {}

    async def post_transaction() -> None:
        async with engine.begin() as connection:
            holder["a_pid"] = await _backend_pid(connection)
            await _post(connection, transaction_id)
            a_posted.set()
            await asyncio.wait_for(b_waiting.wait(), timeout=10)
            # Exiting the context commits the POSTED header.

    async def insert_entry() -> None:
        await asyncio.wait_for(a_posted.wait(), timeout=10)
        async with engine.begin() as connection:
            holder["b_pid"] = await _backend_pid(connection)
            b_pid_ready.set()
            with pytest.raises(Exception, match="posted transaction"):
                await _add_entry(connection, transaction_id, cash_id, "DEBIT", "1.00")
        b_failed.set()

    async def observe_lock_wait() -> None:
        await asyncio.wait_for(b_pid_ready.wait(), timeout=10)
        await _wait_for_lock_wait(
            engine,
            blocker_pid=holder["a_pid"],
            waiter_pid=holder["b_pid"],
        )
        b_waiting.set()

    try:
        results = await asyncio.gather(
            post_transaction(),
            insert_entry(),
            observe_lock_wait(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await asyncio.wait_for(b_failed.wait(), timeout=10)

        async with engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            count = await connection.scalar(
                text("SELECT count(*) FROM ledger_entries WHERE transaction_id = :tid"),
                {"tid": transaction_id},
            )
            assert status == "POSTED"
            assert count == 2
    finally:
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_database_url_is_isolated_test_database(test_dsn: str, migrated: None) -> None:
    """Guard against accidentally pointing this suite at the runtime wallet_db."""
    assert test_dsn.rstrip("/").endswith("/wallet_db_test")


async def test_system_accounts_are_seeded_exactly_once(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT account_type FROM ledger_accounts "
                        "WHERE account_type = ANY(:types)"
                    ),
                    {"types": list(SYSTEM_TYPES)},
                )
            ).scalars().all()
            assert set(rows) == set(SYSTEM_TYPES)

            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO ledger_accounts (account_type, currency, name) "
                        "VALUES ('SYSTEM_CASH_CLEARING', 'INR', 'duplicate')"
                    )
                )
    finally:
        await engine.dispose()


async def test_committed_in_progress_idempotency_records_are_anomalous(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM idempotency_records WHERE status = 'IN_PROGRESS'")
            )
            assert count == 0
    finally:
        await engine.dispose()


async def test_opening_amount_is_exempt_from_operation_cap(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with _tx(engine) as connection:
            transaction_id = await _create_transaction(
                connection, kind="OPENING", amount="2500000.00"
            )
            amount = await connection.scalar(
                text("SELECT amount FROM ledger_transactions WHERE id = :tid"),
                {"tid": transaction_id},
            )
            assert amount == Decimal("2500000.00")
    finally:
        await engine.dispose()


async def test_migration_downgrade_removes_only_new_objects(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    user_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO wallets (user_id, balance, currency) "
                    "VALUES (:uid, 2500000.00, 'INR')"
                ),
                {"uid": user_id},
            )

        try:
            down = _alembic(test_dsn, "downgrade", "0002_create_wallets")
            assert down.returncode == 0, down.stderr
            down_snapshot = await _catalog_snapshot(engine)
            assert not (EXPECTED_TABLES & down_snapshot["tables"])
            assert not (EXPECTED_FUNCTIONS & down_snapshot["functions"])
            assert not (EXPECTED_TRIGGERS & down_snapshot["triggers"])
            assert not (EXPECTED_INDEXES & down_snapshot["indexes"])
            assert not (EXPECTED_FKS & down_snapshot["foreign_keys"])
            assert not any(
                item.startswith("wallets|") and "balance <= 9999999999999999.99" in item
                for item in down_snapshot["checks"]
            )

            async with engine.begin() as connection:
                ledger_table = await connection.scalar(
                    text("SELECT to_regclass('public.ledger_transactions')")
                )
                assert ledger_table is None
                down_version = await connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
                assert down_version == "0002_create_wallets"
                balance = await connection.scalar(
                    text("SELECT balance FROM wallets WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                assert balance == Decimal("2500000.00")
        finally:
            up = _alembic(test_dsn, "upgrade", "head")
            assert up.returncode == 0, up.stderr

        snapshot = await _catalog_snapshot(engine)
        assert snapshot["tables"] >= EXPECTED_TABLES
        assert snapshot["functions"] >= EXPECTED_FUNCTIONS
        assert snapshot["triggers"] >= EXPECTED_TRIGGERS
        assert snapshot["indexes"] >= EXPECTED_INDEXES
        assert snapshot["foreign_keys"] >= EXPECTED_FKS
        for fragment in EXPECTED_CHECK_FRAGMENTS:
            table, needle = fragment.split("|", 1)
            assert any(
                item.startswith(f"{table}|") and needle in item
                for item in snapshot["checks"]
            ), fragment

        async with engine.begin() as connection:
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == "0005_backfill_opening_balances"
            system_count = await connection.scalar(
                text("SELECT count(*) FROM ledger_accounts WHERE account_type = ANY(:types)"),
                {"types": list(SYSTEM_TYPES)},
            )
            assert system_count == 2
            balance = await connection.scalar(
                text("SELECT balance FROM wallets WHERE user_id = :uid"),
                {"uid": user_id},
            )
            assert balance == Decimal("2500000.00")
    finally:
        _ = _alembic(test_dsn, "downgrade", "0004_harden_ledger_invariants")
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM wallets WHERE user_id = :uid"), {"uid": user_id}
            )
        _ = _alembic(test_dsn, "upgrade", "head")
        await engine.dispose()
