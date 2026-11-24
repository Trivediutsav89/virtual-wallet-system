"""Phase 3.2 opening-balance backfill tests against wallet_db_test."""

from __future__ import annotations

import importlib.util
import json
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
MIGRATION_PATH = SERVICE_ROOT / "alembic/versions/0005_backfill_opening_balances.py"
PREVIOUS_REVISION = "0004_harden_ledger_invariants"


def _load_migration() -> object:
    spec = importlib.util.spec_from_file_location("opening_backfill_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()
MIGRATION = migration.MIGRATION  # type: ignore[attr-defined]
GUC_NAME = migration.GUC_NAME  # type: ignore[attr-defined]


@asynccontextmanager
async def _tx(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            if transaction.is_active:
                await transaction.rollback()


def _alembic(test_dsn: str, command: str, revision: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "WALLET_DATABASE_URL": test_dsn}
    return subprocess.run(  # noqa: S603 - fixed arguments from this module
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision],
        cwd=SERVICE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_backfill(test_dsn: str) -> None:
    down = _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
    assert down.returncode == 0, down.stderr
    up = _alembic(test_dsn, "upgrade", "head")
    assert up.returncode == 0, up.stderr


def _restore_head(test_dsn: str) -> None:
    up = _alembic(test_dsn, "upgrade", "head")
    assert up.returncode == 0, up.stderr


async def _current_revision(test_dsn: str) -> str:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return str(await connection.scalar(text("SELECT version_num FROM alembic_version")))
    finally:
        await engine.dispose()


async def _truncate_and_reseed(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE ledger_entries, ledger_transactions, "
                "idempotency_records, ledger_accounts, ledger_opening_backfills, "
                "wallets RESTART IDENTITY CASCADE"
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


async def _seed_wallet(
    connection: AsyncConnection, balance: str = "0.00", currency: str = "INR"
) -> uuid.UUID:
    return uuid.UUID(
        str(
            await connection.scalar(
                text(
                    "INSERT INTO wallets (user_id, balance, currency) "
                    "VALUES (:uid, :balance, :currency) RETURNING id"
                ),
                {"uid": uuid.uuid4(), "balance": Decimal(balance), "currency": currency},
            )
        )
    )


async def _system_account_id(connection: AsyncConnection) -> uuid.UUID:
    return uuid.UUID(
        str(
            await connection.scalar(
                text("SELECT id FROM ledger_accounts WHERE account_type='SYSTEM_OPENING_EQUITY'")
            )
        )
    )


async def _posted_transaction(
    connection: AsyncConnection,
    *,
    account_id: uuid.UUID,
    system_id: uuid.UUID,
    amount: str,
    kind: str = "DEPOSIT",
    metadata: dict[str, object] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    transaction_id = uuid.uuid4()
    debit_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    payload = metadata or {}
    await connection.execute(
        text(
            "INSERT INTO ledger_transactions "
            "(id, kind, amount, currency, status, posted_at, metadata) "
            "VALUES (:id, :kind, :amount, 'INR', 'PENDING', NULL, CAST(:meta AS jsonb))"
        ),
        {
            "id": transaction_id,
            "kind": kind,
            "amount": Decimal(amount),
            "meta": json.dumps(payload),
        },
    )
    for entry_id, direction, target in (
        (debit_id, "DEBIT", system_id),
        (credit_id, "CREDIT", account_id),
    ):
        await connection.execute(
            text(
                "INSERT INTO ledger_entries "
                "(id, transaction_id, account_id, direction, amount, currency) "
                "VALUES (:id, :tid, :aid, :dir, :amount, 'INR')"
            ),
            {
                "id": entry_id,
                "tid": transaction_id,
                "aid": target,
                "dir": direction,
                "amount": Decimal(amount),
            },
        )
    await connection.execute(
        text("UPDATE ledger_transactions SET status='POSTED', posted_at=now() WHERE id=:tid"),
        {"tid": transaction_id},
    )
    return transaction_id, debit_id, credit_id


async def test_backfill_positive_wallet_creates_account_transaction_and_entries(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            wallet_id = await _seed_wallet(connection, "500.00")
        _run_backfill(test_dsn)

        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        async with engine.connect() as connection:
            marker = (
                await connection.execute(
                    text(
                        "SELECT account_id, transaction_id, opening_balance "
                        "FROM ledger_opening_backfills WHERE wallet_id=:wid"
                    ),
                    {"wid": wallet_id},
                )
            ).one()
            assert marker.account_id == ids["account"]
            assert marker.transaction_id == ids["transaction"]
            assert marker.opening_balance == Decimal("500.00")

            account_type = await connection.scalar(
                text("SELECT account_type FROM ledger_accounts WHERE id=:aid"),
                {"aid": ids["account"]},
            )
            assert account_type == "USER_WALLET"

            tx = (
                await connection.execute(
                    text(
                        "SELECT status, amount, currency, metadata "
                        "FROM ledger_transactions WHERE id=:tid"
                    ),
                    {"tid": ids["transaction"]},
                )
            ).one()
            assert tx.status == "POSTED"
            assert tx.amount == Decimal("500.00")
            assert tx.currency == "INR"
            assert tx.metadata["migration"] == MIGRATION
            assert tx.metadata["wallet_id"] == str(wallet_id)
            assert tx.metadata["wallet_account_id"] == str(ids["account"])
            assert tx.metadata["debit_entry_id"] == str(ids["debit_entry"])
            assert tx.metadata["credit_entry_id"] == str(ids["credit_entry"])

            entries = (
                await connection.execute(
                    text(
                        "SELECT id, direction, account_id, amount FROM ledger_entries "
                        "WHERE transaction_id=:tid ORDER BY direction"
                    ),
                    {"tid": ids["transaction"]},
                )
            ).all()
            assert len(entries) == 2
            assert entries[0].direction == "CREDIT"
            assert entries[0].account_id == ids["account"]
            assert entries[1].direction == "DEBIT"
            assert entries[1].account_id == await _system_account_id(connection)
            assert all(row.amount == Decimal("500.00") for row in entries)

            balance = await connection.scalar(
                text("SELECT balance FROM wallets WHERE id=:wid"), {"wid": wallet_id}
            )
            assert balance == Decimal("500.00")
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_backfill_zero_wallet_creates_account_without_transaction(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            wallet_id = await _seed_wallet(connection, "0.00")
        _run_backfill(test_dsn)

        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        async with engine.connect() as connection:
            marker = (
                await connection.execute(
                    text(
                        "SELECT account_id, transaction_id, opening_balance "
                        "FROM ledger_opening_backfills WHERE wallet_id=:wid"
                    ),
                    {"wid": wallet_id},
                )
            ).one()
            assert marker.account_id == ids["account"]
            assert marker.transaction_id is None
            assert marker.opening_balance == Decimal("0.00")
            count = await connection.scalar(
                text(
                    "SELECT count(*) FROM ledger_transactions "
                    "WHERE kind='OPENING' AND metadata->>'wallet_id' = :wid"
                ),
                {"wid": str(wallet_id)},
            )
            assert count == 0
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_backfill_multiple_wallets_and_large_balance_preserved(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            wallets = {
                "small": await _seed_wallet(connection, "125.50"),
                "large": await _seed_wallet(connection, "2500000.00"),
                "zero": await _seed_wallet(connection, "0.00"),
            }
        _run_backfill(test_dsn)

        async with engine.connect() as connection:
            markers = (
                await connection.execute(
                    text(
                        "SELECT wallet_id, transaction_id, opening_balance "
                        "FROM ledger_opening_backfills"
                    )
                )
            ).all()
            assert len(markers) == 3
            by_wallet = {row.wallet_id: row for row in markers}
            assert by_wallet[wallets["large"]].opening_balance == Decimal("2500000.00")
            assert by_wallet[wallets["large"]].transaction_id is not None
            assert by_wallet[wallets["zero"]].transaction_id is None
            for name, expected in (("small", "125.50"), ("large", "2500000.00"), ("zero", "0.00")):
                balance = await connection.scalar(
                    text("SELECT balance FROM wallets WHERE id=:wid"), {"wid": wallets[name]}
                )
                assert balance == Decimal(expected)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_backfill_marker_table_has_one_row_per_wallet(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            await _seed_wallet(connection, "10.00")
            await _seed_wallet(connection, "0.00")
        _run_backfill(test_dsn)

        async with engine.connect() as connection:
            wallet_count = await connection.scalar(text("SELECT count(*) FROM wallets"))
            marker_count = await connection.scalar(
                text("SELECT count(*) FROM ledger_opening_backfills")
            )
            assert wallet_count == marker_count == 2
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_backfill_reconciles_wallet_balances(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            await _seed_wallet(connection, "321.10")
            await _seed_wallet(connection, "0.00")
        _run_backfill(test_dsn)

        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT w.id AS wallet_id, w.balance AS projection, "
                        "COALESCE(SUM(CASE WHEN e.direction='CREDIT' "
                        "THEN e.amount ELSE -e.amount END),0) AS ledger_balance "
                        "FROM wallets w "
                        "LEFT JOIN ledger_accounts la "
                        "ON la.wallet_id=w.id AND la.account_type='USER_WALLET' "
                        "LEFT JOIN ledger_entries e ON e.account_id=la.id "
                        "GROUP BY w.id, w.balance"
                    )
                )
            ).all()
            assert len(rows) == 2
            assert all(row.projection == row.ledger_balance for row in rows)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_backfill_duplicate_protection(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            first = await _seed_wallet(connection, "10.00")
            second = await _seed_wallet(connection, "20.00")
        _run_backfill(test_dsn)

        async with engine.begin() as connection:
            marker = (
                await connection.execute(
                    text("SELECT account_id FROM ledger_opening_backfills WHERE wallet_id=:wid"),
                    {"wid": first},
                )
            ).one()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO ledger_opening_backfills "
                        "(migration, wallet_id, account_id, transaction_id, opening_balance, "
                        "account_created_by_migration) "
                        "VALUES (:m, :wid, :aid, NULL, 0.00, false)"
                    ),
                    {"m": MIGRATION, "wid": second, "aid": marker.account_id},
                )
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_failed_backfill_leaves_no_partial_records(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        down = _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
        assert down.returncode == 0, down.stderr
        async with engine.begin() as connection:
            wallet_id = await _seed_wallet(connection, "100.00")
            ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
            await connection.execute(
                text(
                    "INSERT INTO ledger_transactions "
                    "(id, kind, amount, currency, status, metadata) "
                    "VALUES (:id, 'DEPOSIT', 1.00, 'INR', 'PENDING', '{}'::jsonb)"
                ),
                {"id": ids["transaction"]},
            )
        failed = _alembic(test_dsn, "upgrade", "head")
        assert failed.returncode != 0
        assert await _current_revision(test_dsn) == PREVIOUS_REVISION

        async with engine.connect() as connection:
            marker_table = await connection.scalar(
                text("SELECT to_regclass('public.ledger_opening_backfills')")
            )
            assert marker_table is None
            balance = await connection.scalar(
                text("SELECT balance FROM wallets WHERE id=:wid"), {"wid": wallet_id}
            )
            assert balance == Decimal("100.00")

        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM ledger_transactions WHERE id=:tid"), {"tid": ids["transaction"]}
            )
        _restore_head(test_dsn)
        async with engine.connect() as connection:
            count = await connection.scalar(text("SELECT count(*) FROM ledger_opening_backfills"))
            assert count == 1
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def _create_unrelated_posted(
    connection: AsyncConnection, amount: str = "75.00"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    wallet_id = await _seed_wallet(connection, amount)
    account_id = uuid.uuid4()
    system_id = await _system_account_id(connection)
    await connection.execute(
        text(
            "INSERT INTO ledger_accounts (id, account_type, wallet_id, currency, name) "
            "VALUES (:id, 'USER_WALLET', :wid, 'INR', 'unrelated')"
        ),
        {"id": account_id, "wid": wallet_id},
    )
    transaction_id, debit_id, _credit_id = await _posted_transaction(
        connection, account_id=account_id, system_id=system_id, amount=amount
    )
    return wallet_id, account_id, transaction_id, debit_id


async def test_downgrade_removes_only_backfill_records(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            positive = await _seed_wallet(connection, "200.00")
            zero = await _seed_wallet(connection, "0.00")
        _run_backfill(test_dsn)

        down = _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
        assert down.returncode == 0, down.stderr
        assert await _current_revision(test_dsn) == PREVIOUS_REVISION

        async with engine.connect() as connection:
            marker_table = await connection.scalar(
                text("SELECT to_regclass('public.ledger_opening_backfills')")
            )
            assert marker_table is None
            accounts = await connection.scalar(
                text("SELECT count(*) FROM ledger_accounts WHERE wallet_id IN (:a, :b)"),
                {"a": positive, "b": zero},
            )
            assert accounts == 0
            openings = await connection.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE kind='OPENING'")
            )
            assert openings == 0
            for wallet_id, expected in ((positive, "200.00"), (zero, "0.00")):
                balance = await connection.scalar(
                    text("SELECT balance FROM wallets WHERE id=:wid"), {"wid": wallet_id}
                )
                assert balance == Decimal(expected)
        _restore_head(test_dsn)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_zero_balance_account_is_removed(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            zero = await _seed_wallet(connection, "0.00")
        _run_backfill(test_dsn)
        account_id = migration._ids(zero)["account"]  # type: ignore[attr-defined]
        _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)

        async with engine.connect() as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM ledger_accounts WHERE id=:aid"), {"aid": account_id}
            )
            assert count == 0
        _restore_head(test_dsn)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_preserves_system_accounts(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        down = _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
        assert down.returncode == 0, down.stderr
        up = _alembic(test_dsn, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        down = _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
        assert down.returncode == 0, down.stderr

        async with engine.connect() as connection:
            types = (
                await connection.execute(
                    text(
                        "SELECT account_type FROM ledger_accounts "
                        "WHERE account_type <> 'USER_WALLET'"
                    )
                )
            ).scalars().all()
            assert set(types) == {"SYSTEM_CASH_CLEARING", "SYSTEM_OPENING_EQUITY"}
        _restore_head(test_dsn)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_unrelated_posted_records_survive_backfill_and_downgrade(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            _wallet, _account, transaction_id, debit_id = await _create_unrelated_posted(connection)
        _run_backfill(test_dsn)

        async with engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id=:tid"),
                {"tid": transaction_id},
            )
            entry_count = await connection.scalar(
                text("SELECT count(*) FROM ledger_entries WHERE transaction_id=:tid"),
                {"tid": transaction_id},
            )
            assert status == "POSTED"
            assert entry_count == 2

        _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
        async with engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM ledger_transactions WHERE id=:tid"),
                {"tid": transaction_id},
            )
            entry = await connection.scalar(
                text("SELECT id FROM ledger_entries WHERE id=:eid"), {"eid": debit_id}
            )
            assert status == "POSTED"
            assert entry == debit_id
        _restore_head(test_dsn)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_reupgrade_creates_no_duplicates(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            await _seed_wallet(connection, "50.00")
        _run_backfill(test_dsn)
        _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
        _restore_head(test_dsn)

        async with engine.connect() as connection:
            markers = await connection.scalar(text("SELECT count(*) FROM ledger_opening_backfills"))
            openings = await connection.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE kind='OPENING'")
            )
            assert markers == 1
            assert openings == 1
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_guc_alone_cannot_delete_unrelated_posted_entry(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            _wallet, _account, _transaction, debit_id = await _create_unrelated_posted(connection)
        _run_backfill(test_dsn)

        async with _tx(engine) as connection:
            await connection.execute(text(f"SET LOCAL {GUC_NAME} = '{MIGRATION}'"))
            with pytest.raises(Exception, match="immutable"):
                await connection.execute(
                    text("DELETE FROM ledger_entries WHERE id=:eid"), {"eid": debit_id}
                )
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_guc_alone_cannot_delete_unrelated_posted_transaction(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            _wallet, _account, transaction_id, _debit = await _create_unrelated_posted(connection)
        _run_backfill(test_dsn)

        async with _tx(engine) as connection:
            await connection.execute(text(f"SET LOCAL {GUC_NAME} = '{MIGRATION}'"))
            with pytest.raises(Exception, match="cannot be deleted"):
                await connection.execute(
                    text("DELETE FROM ledger_transactions WHERE id=:tid"), {"tid": transaction_id}
                )
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_marker_for_one_wallet_cannot_authorize_other_wallet(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            await _seed_wallet(connection, "10.00")
            other = await _seed_wallet(connection, "20.00")
            other_ids = migration._ids(other)  # type: ignore[attr-defined]
            system_id = await _system_account_id(connection)
            await connection.execute(
                text(
                    "INSERT INTO ledger_accounts (id, account_type, wallet_id, currency, name) "
                    "VALUES (:id, 'USER_WALLET', :wid, 'INR', 'other')"
                ),
                {"id": other_ids["account"], "wid": other},
            )
            metadata = {
                "migration": MIGRATION,
                "wallet_id": str(other),
                "wallet_account_id": str(other_ids["account"]),
                "system_account_id": str(system_id),
                "debit_entry_id": str(other_ids["debit_entry"]),
                "credit_entry_id": str(other_ids["credit_entry"]),
                "opening_balance": "20.00",
            }
            _fake_tx, fake_debit_id, _fake_credit_id = await _posted_transaction(
                connection,
                account_id=other_ids["account"],
                system_id=system_id,
                amount="20.00",
                kind="OPENING",
                metadata=metadata,
            )
        _run_backfill(test_dsn)

        async with _tx(engine) as connection:
            await connection.execute(text(f"SET LOCAL {GUC_NAME} = '{MIGRATION}'"))
            with pytest.raises(Exception, match="immutable"):
                await connection.execute(
                    text("DELETE FROM ledger_entries WHERE id=:eid"),
                    {"eid": fake_debit_id},
                )
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_mismatched_transaction_id_cannot_authorize_deletion(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        async with engine.begin() as connection:
            wallet_id = await _seed_wallet(connection, "30.00")
            ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
            system_id = await _system_account_id(connection)
            await connection.execute(
                text(
                    "INSERT INTO ledger_accounts (id, account_type, wallet_id, currency, name) "
                    "VALUES (:id, 'USER_WALLET', :wid, 'INR', 'mismatch')"
                ),
                {"id": ids["account"], "wid": wallet_id},
            )
            metadata = {
                "migration": MIGRATION,
                "wallet_id": str(wallet_id),
                "wallet_account_id": str(ids["account"]),
                "system_account_id": str(system_id),
                "debit_entry_id": str(ids["debit_entry"]),
                "credit_entry_id": str(ids["credit_entry"]),
                "opening_balance": "30.00",
            }
            target_tx, _debit, _credit = await _posted_transaction(
                connection,
                account_id=ids["account"],
                system_id=system_id,
                amount="30.00",
                kind="OPENING",
                metadata=metadata,
            )
            dummy_tx = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO ledger_transactions "
                    "(id, kind, amount, currency, status, metadata) "
                    "VALUES (:id, 'DEPOSIT', 1.00, 'INR', 'PENDING', '{}'::jsonb)"
                ),
                {"id": dummy_tx},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledger_opening_backfills "
                    "(migration, wallet_id, account_id, transaction_id, opening_balance, "
                    "account_created_by_migration) "
                    "VALUES (:m, :wid, :aid, :tid, 30.00, false)"
                ),
                {"m": MIGRATION, "wid": wallet_id, "aid": ids["account"], "tid": dummy_tx},
            )

        async with _tx(engine) as connection:
            await connection.execute(text(f"SET LOCAL {GUC_NAME} = '{MIGRATION}'"))
            with pytest.raises(Exception, match="immutable"):
                await connection.execute(
                    text("DELETE FROM ledger_entries WHERE transaction_id=:tid"),
                    {"tid": target_tx},
                )
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_ledger_triggers_remain_enabled_after_upgrade(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        _run_backfill(test_dsn)
        _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
        _restore_head(test_dsn)
        async with engine.connect() as connection:
            names = (
                await connection.execute(
                    text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname")
                )
            ).scalars().all()
            assert "trg_ledger_entries_immutable" in names
            assert "trg_ledger_transactions_no_delete" in names
            assert "trg_ledger_accounts_identity" in names
            assert "ctrg_assert_posted_transaction_invariants" in names
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def _seed_backfilled_wallet(
    test_dsn: str, engine: AsyncEngine, balance: str = "30.00"
) -> uuid.UUID:
    _run_backfill(test_dsn)
    async with engine.begin() as connection:
        wallet_id = await _seed_wallet(connection, balance)
    _run_backfill(test_dsn)
    return wallet_id


async def _snapshot_opening_records(engine: AsyncEngine, wallet_id: uuid.UUID) -> dict[str, int]:
    async with engine.connect() as connection:
        marker_count = await connection.scalar(
            text("SELECT count(*) FROM ledger_opening_backfills WHERE wallet_id = :wid"),
            {"wid": wallet_id},
        )
        account_count = await connection.scalar(
            text("SELECT count(*) FROM ledger_accounts WHERE wallet_id = :wid"),
            {"wid": wallet_id},
        )
        transaction_count = await connection.scalar(
            text(
                "SELECT count(*) FROM ledger_transactions t "
                "JOIN ledger_opening_backfills m ON m.transaction_id = t.id "
                "WHERE m.wallet_id = :wid"
            ),
            {"wid": wallet_id},
        )
        entry_count = await connection.scalar(
            text(
                "SELECT count(*) FROM ledger_entries e "
                "JOIN ledger_opening_backfills m ON m.transaction_id = e.transaction_id "
                "WHERE m.wallet_id = :wid"
            ),
            {"wid": wallet_id},
        )
    return {
        "markers": int(marker_count or 0),
        "accounts": int(account_count or 0),
        "transactions": int(transaction_count or 0),
        "entries": int(entry_count or 0),
    }


async def _assert_downgrade_failed_and_intact(
    test_dsn: str,
    engine: AsyncEngine,
    wallet_id: uuid.UUID,
    before: dict[str, int],
) -> None:
    failed = _alembic(test_dsn, "downgrade", PREVIOUS_REVISION)
    assert failed.returncode != 0
    assert await _current_revision(test_dsn) == "0005_backfill_opening_balances"
    after = await _snapshot_opening_records(engine, wallet_id)
    assert after == before


def _anomaly_metadata(
    wallet_id: uuid.UUID,
    account_id: uuid.UUID,
    system_id: uuid.UUID,
    debit_id: uuid.UUID,
    credit_id: uuid.UUID,
    amount: str,
) -> dict[str, object]:
    return {
        "migration": MIGRATION,
        "wallet_id": str(wallet_id),
        "wallet_account_id": str(account_id),
        "system_account_id": str(system_id),
        "debit_entry_id": str(debit_id),
        "credit_entry_id": str(credit_id),
        "opening_balance": amount,
    }


async def _install_anomalous_opening(
    engine: AsyncEngine,
    wallet_id: uuid.UUID,
    *,
    amount: str,
    metadata: dict[str, object],
    entries: list[tuple[uuid.UUID, uuid.UUID, str, str]],
) -> None:
    ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
    async with engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL {GUC_NAME} = '{MIGRATION}'"))
        await connection.execute(
            text("DELETE FROM ledger_entries WHERE transaction_id = :tid"),
            {"tid": ids["transaction"]},
        )
        await connection.execute(
            text("DELETE FROM ledger_transactions WHERE id = :tid"),
            {"tid": ids["transaction"]},
        )
        transaction_id = uuid.uuid4()
        await connection.execute(
            text(
                "INSERT INTO ledger_transactions "
                "(id, kind, amount, currency, status, posted_at, metadata) "
                "VALUES (:id, 'OPENING', :amount, 'INR', 'PENDING', NULL, CAST(:meta AS jsonb))"
            ),
            {"id": transaction_id, "amount": Decimal(amount), "meta": json.dumps(metadata)},
        )
        for entry_id, account_id, direction, entry_amount in entries:
            await connection.execute(
                text(
                    "INSERT INTO ledger_entries "
                    "(id, transaction_id, account_id, direction, amount, currency) "
                    "VALUES (:id, :tid, :aid, :dir, :amount, 'INR')"
                ),
                {
                    "id": entry_id,
                    "tid": transaction_id,
                    "aid": account_id,
                    "dir": direction,
                    "amount": Decimal(entry_amount),
                },
            )
        await connection.execute(
            text(
                "UPDATE ledger_transactions "
                "SET status = 'POSTED', posted_at = now() WHERE id = :tid"
            ),
            {"tid": transaction_id},
        )
        await connection.execute(
            text(
                "UPDATE ledger_opening_backfills "
                "SET transaction_id = :tid WHERE wallet_id = :wid"
            ),
            {"tid": transaction_id, "wid": wallet_id},
        )


async def _delete_one_opening_entry(engine: AsyncEngine, entry_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL {GUC_NAME} = '{MIGRATION}'"))
        await connection.execute(
            text("DELETE FROM ledger_entries WHERE id = :eid"), {"eid": entry_id}
        )


async def _system_id(engine: AsyncEngine) -> uuid.UUID:
    async with engine.connect() as connection:
        return await _system_account_id(connection)


async def test_downgrade_fails_when_debit_entry_metadata_missing(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        del metadata["debit_entry_id"]
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_credit_entry_metadata_missing(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        del metadata["credit_entry_id"]
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_metadata_uuid_is_invalid(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        metadata["debit_entry_id"] = "not-a-uuid"
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_wallet_or_account_metadata_is_wrong(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        metadata["wallet_account_id"] = str(uuid.uuid4())
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_debit_entry_is_missing(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        await _delete_one_opening_entry(engine, debit_id)
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_credit_entry_is_missing(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        await _delete_one_opening_entry(engine, credit_id)
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_with_unexpected_third_entry(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        extra_debit_id, extra_credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "20.00"),
                (credit_id, ids["account"], "CREDIT", "20.00"),
                (extra_debit_id, system_id, "DEBIT", "10.00"),
                (extra_credit_id, ids["account"], "CREDIT", "10.00"),
            ],
        )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_metadata_entry_ids_are_swapped(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, credit_id, debit_id, "30.00"
        )
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_actual_debit_entry_has_wrong_account(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, ids["account"], "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_actual_credit_entry_has_wrong_account(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, system_id, "CREDIT", "30.00"),
            ],
        )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_amount_mismatch(test_dsn: str, migrated: None) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        ids = migration._ids(wallet_id)  # type: ignore[attr-defined]
        system_id = await _system_id(engine)
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        metadata = _anomaly_metadata(
            wallet_id, ids["account"], system_id, debit_id, credit_id, "30.00"
        )
        await _install_anomalous_opening(
            engine,
            wallet_id,
            amount="30.00",
            metadata=metadata,
            entries=[
                (debit_id, system_id, "DEBIT", "30.00"),
                (credit_id, ids["account"], "CREDIT", "30.00"),
            ],
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE ledger_opening_backfills "
                    "SET opening_balance = 40.00 WHERE wallet_id = :wid"
                ),
                {"wid": wallet_id},
            )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()


async def test_downgrade_fails_when_entry_currency_is_wrong(
    test_dsn: str, migrated: None
) -> None:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    constraint_name: str | None = None
    constraint_def: str | None = None
    try:
        wallet_id = await _seed_backfilled_wallet(test_dsn, engine)
        async with engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid = 'ledger_entries'::regclass AND contype = 'c' "
                        "AND pg_get_constraintdef(oid) LIKE '%currency%'"
                    )
                )
            ).first()
            assert row is not None
            constraint_name, constraint_def = row[0], row[1]
            await connection.exec_driver_sql(
                f'ALTER TABLE ledger_entries DROP CONSTRAINT "{constraint_name}"'
            )
            await connection.exec_driver_sql("ALTER TABLE ledger_entries DISABLE TRIGGER USER")
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE ledger_entries SET currency = 'USD' WHERE currency = 'INR'")
            )
        before = await _snapshot_opening_records(engine, wallet_id)
        await _assert_downgrade_failed_and_intact(test_dsn, engine, wallet_id, before)
    finally:
        async with engine.begin() as connection:
            try:
                await connection.execute(
                    text("UPDATE ledger_entries SET currency = 'INR' WHERE currency <> 'INR'")
                )
                if constraint_name and constraint_def:
                    add_sql = (
                        f'ALTER TABLE ledger_entries ADD CONSTRAINT "{constraint_name}" '
                        f"{constraint_def}"
                    )
                    await connection.exec_driver_sql(add_sql)
            finally:
                await connection.exec_driver_sql(
                    "ALTER TABLE ledger_entries ENABLE TRIGGER USER"
                )
        _restore_head(test_dsn)
        await _truncate_and_reseed(engine)
        await engine.dispose()
