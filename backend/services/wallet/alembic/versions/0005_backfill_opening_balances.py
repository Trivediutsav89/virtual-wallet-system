"""backfill opening balances into the double-entry ledger

Revision ID: 0005_backfill_opening_balances
Revises: 0004_harden_ledger_invariants
Create Date: 2026-11-24

Creates one marker per wallet, one USER_WALLET account per wallet, and one
posted OPENING transaction with two balanced entries per positive-balance
wallet. Zero-balance wallets receive an account and a marker with a NULL
transaction_id.

Downgrade is authorized by a GUC plus an exact marker/metadata match. The GUC
alone never permits deletion of posted ledger records.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0005_backfill_opening_balances"
down_revision: str | None = "0004_harden_ledger_invariants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION = "0005_backfill_opening_balances"
GUC_NAME = "app.phase3_2_backfill"
NAMESPACE = uuid.UUID("4f2c8b1e-6a0d-4c7f-9e21-0b5d8a7f3c10")
INR = "INR"
USER_WALLET = "USER_WALLET"
SYSTEM_OPENING_EQUITY = "SYSTEM_OPENING_EQUITY"
MAX_BALANCE = Decimal("9999999999999999.99")


def _ids(wallet_id: uuid.UUID) -> dict[str, uuid.UUID]:
    return {
        "account": uuid.uuid5(NAMESPACE, f"account:{wallet_id}"),
        "transaction": uuid.uuid5(NAMESPACE, f"opening:{wallet_id}"),
        "debit_entry": uuid.uuid5(NAMESPACE, f"opening:debit:{wallet_id}"),
        "credit_entry": uuid.uuid5(NAMESPACE, f"opening:credit:{wallet_id}"),
    }


def _validate_wallet_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Validate balances and currencies before any ledger write."""
    seen: set[uuid.UUID] = set()
    for row in rows:
        wallet_id = row["id"]
        if wallet_id in seen:
            raise RuntimeError(f"duplicate wallet id {wallet_id}")
        seen.add(wallet_id)
        balance = row["balance"]
        if not isinstance(balance, Decimal):
            raise RuntimeError(f"wallet {wallet_id} balance is not Decimal")
        if balance < 0:
            raise RuntimeError(f"wallet {wallet_id} has negative balance {balance}")
        if balance > MAX_BALANCE:
            raise RuntimeError(f"wallet {wallet_id} balance {balance} exceeds NUMERIC(18,2)")
        if balance.as_tuple().exponent < -2:
            raise RuntimeError(f"wallet {wallet_id} balance {balance} has invalid scale")
        if row["currency"] != INR:
            raise RuntimeError(f"wallet {wallet_id} has unsupported currency {row['currency']}")


def _create_marker_table() -> None:
    op.create_table(
        "ledger_opening_backfills",
        sa.Column("migration", sa.String(length=64), nullable=False),
        sa.Column(
            "wallet_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("wallets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("ledger_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "transaction_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                "ledger_transactions.id",
                ondelete="NO ACTION",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=True,
        ),
        sa.Column("opening_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("account_created_by_migration", sa.Boolean, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "opening_balance >= 0",
            name="ck_ledger_opening_backfills_balance_nonnegative",
        ),
        sa.CheckConstraint(
            "(opening_balance > 0 AND transaction_id IS NOT NULL) OR "
            "(opening_balance = 0 AND transaction_id IS NULL)",
            name="ck_ledger_opening_backfills_transaction_presence",
        ),
        sa.PrimaryKeyConstraint("migration", "wallet_id", name="pk_ledger_opening_backfills"),
        sa.UniqueConstraint(
            "migration", "account_id", name="uq_ledger_opening_backfills_account"
        ),
    )
    op.create_index(
        "ux_ledger_opening_backfills_transaction",
        "ledger_opening_backfills",
        ["migration", "transaction_id"],
        unique=True,
        postgresql_where=sa.text("transaction_id IS NOT NULL"),
    )
    op.create_index(
        "ix_ledger_opening_backfills_account",
        "ledger_opening_backfills",
        ["account_id"],
    )


def _authorization_functions() -> str:
    return """
        CREATE OR REPLACE FUNCTION ledger_backfill_entry_delete_authorized(p_entry_id uuid)
        RETURNS boolean AS $$
        DECLARE
            v_tx ledger_transactions%ROWTYPE;
            v_marker ledger_opening_backfills%ROWTYPE;
            v_entry ledger_entries%ROWTYPE;
            v_system_account_id uuid;
            v_debit_id uuid;
            v_credit_id uuid;
            v_count integer;
            v_expected integer;
        BEGIN
            IF current_setting('__GUC__', true) IS DISTINCT FROM '__MIGRATION__' THEN
                RETURN false;
            END IF;

            SELECT * INTO v_entry FROM ledger_entries WHERE id = p_entry_id;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            SELECT * INTO v_tx FROM ledger_transactions WHERE id = v_entry.transaction_id;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            IF v_tx.kind <> 'OPENING' OR v_tx.status <> 'POSTED' OR v_tx.currency <> 'INR' THEN
                RETURN false;
            END IF;
            IF v_tx.metadata->>'migration' IS DISTINCT FROM '__MIGRATION__' THEN
                RETURN false;
            END IF;

            SELECT * INTO v_marker
            FROM ledger_opening_backfills
            WHERE migration = '__MIGRATION__'
              AND transaction_id = v_tx.id
              AND wallet_id = (v_tx.metadata->>'wallet_id')::uuid
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            SELECT id INTO v_system_account_id
            FROM ledger_accounts
            WHERE account_type = 'SYSTEM_OPENING_EQUITY' AND currency = v_tx.currency;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            v_debit_id := (v_tx.metadata->>'debit_entry_id')::uuid;
            v_credit_id := (v_tx.metadata->>'credit_entry_id')::uuid;

            IF p_entry_id NOT IN (v_debit_id, v_credit_id) THEN
                RETURN false;
            END IF;
            IF v_entry.amount <> v_marker.opening_balance OR v_entry.amount <> v_tx.amount THEN
                RETURN false;
            END IF;
            IF v_entry.currency <> v_tx.currency THEN
                RETURN false;
            END IF;

            IF v_tx.metadata->>'wallet_account_id' IS DISTINCT FROM v_marker.account_id::text THEN
                RETURN false;
            END IF;
            IF v_tx.metadata->>'system_account_id' IS DISTINCT FROM v_system_account_id::text THEN
                RETURN false;
            END IF;

            IF p_entry_id = v_debit_id THEN
                IF v_entry.direction <> 'DEBIT' OR v_entry.account_id <> v_system_account_id THEN
                    RETURN false;
                END IF;
            ELSE
                IF v_entry.direction <> 'CREDIT' OR v_entry.account_id <> v_marker.account_id THEN
                    RETURN false;
                END IF;
            END IF;

            SELECT count(*), count(*) FILTER (WHERE id IN (v_debit_id, v_credit_id))
            INTO v_count, v_expected
            FROM ledger_entries
            WHERE transaction_id = v_tx.id;
            IF v_count NOT IN (1, 2) OR v_expected <> v_count THEN
                RETURN false;
            END IF;

            RETURN true;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION ledger_backfill_transaction_delete_authorized(
            p_transaction_id uuid
        )
        RETURNS boolean AS $$
        DECLARE
            v_tx ledger_transactions%ROWTYPE;
            v_marker ledger_opening_backfills%ROWTYPE;
            v_account ledger_accounts%ROWTYPE;
            v_system_account_id uuid;
        BEGIN
            IF current_setting('__GUC__', true) IS DISTINCT FROM '__MIGRATION__' THEN
                RETURN false;
            END IF;

            SELECT * INTO v_tx FROM ledger_transactions WHERE id = p_transaction_id;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            IF v_tx.kind <> 'OPENING' OR v_tx.status <> 'POSTED' OR v_tx.currency <> 'INR' THEN
                RETURN false;
            END IF;
            IF v_tx.metadata->>'migration' IS DISTINCT FROM '__MIGRATION__' THEN
                RETURN false;
            END IF;

            SELECT * INTO v_marker
            FROM ledger_opening_backfills
            WHERE migration = '__MIGRATION__'
              AND transaction_id = v_tx.id
              AND wallet_id = (v_tx.metadata->>'wallet_id')::uuid
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            IF v_tx.amount <> v_marker.opening_balance THEN
                RETURN false;
            END IF;
            IF v_tx.metadata->>'wallet_account_id' IS DISTINCT FROM v_marker.account_id::text THEN
                RETURN false;
            END IF;

            SELECT * INTO v_account FROM ledger_accounts WHERE id = v_marker.account_id;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            IF v_account.account_type <> 'USER_WALLET'
               OR v_account.wallet_id <> v_marker.wallet_id
               OR v_account.currency <> v_tx.currency THEN
                RETURN false;
            END IF;

            SELECT id INTO v_system_account_id
            FROM ledger_accounts
            WHERE account_type = 'SYSTEM_OPENING_EQUITY' AND currency = v_tx.currency;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            IF v_tx.metadata->>'system_account_id' IS DISTINCT FROM v_system_account_id::text THEN
                RETURN false;
            END IF;

            IF EXISTS (SELECT 1 FROM ledger_entries WHERE transaction_id = v_tx.id) THEN
                RETURN false;
            END IF;

            RETURN true;
        END;
        $$ LANGUAGE plpgsql;
    """.replace("__GUC__", GUC_NAME).replace("__MIGRATION__", MIGRATION)


def _execute_plpgsql_block(sql: str) -> None:
    """Execute each CREATE FUNCTION statement separately for asyncpg."""
    for block in sql.split("$$ LANGUAGE plpgsql;"):
        statement = block.strip()
        if statement:
            op.execute(statement + " $$ LANGUAGE plpgsql;")


def _hardened_entry_mutation_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION reject_ledger_entry_mutation()
        RETURNS trigger AS $$
        DECLARE
            v_status text;
            v_tx_currency varchar(3);
            v_account_currency varchar(3);
        BEGIN
            IF TG_OP = 'UPDATE'
               AND NEW.transaction_id IS DISTINCT FROM OLD.transaction_id THEN
                RAISE EXCEPTION 'ledger entry transaction cannot be changed';
            END IF;

            SELECT status INTO v_status
            FROM ledger_transactions
            WHERE id = OLD.transaction_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'invalid ledger transaction';
            END IF;
            IF v_status = 'POSTED' THEN
                IF TG_OP = 'DELETE'
                   AND ledger_backfill_entry_delete_authorized(OLD.id) THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'posted ledger entries are immutable';
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM ledger_transactions WHERE id = NEW.transaction_id
            ) THEN
                RAISE EXCEPTION 'invalid ledger transaction';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM ledger_accounts WHERE id = NEW.account_id
            ) THEN
                RAISE EXCEPTION 'invalid ledger account';
            END IF;

            SELECT currency INTO v_tx_currency
            FROM ledger_transactions WHERE id = NEW.transaction_id;
            SELECT currency INTO v_account_currency
            FROM ledger_accounts WHERE id = NEW.account_id;

            IF NEW.currency <> v_tx_currency THEN
                RAISE EXCEPTION 'entry currency does not match transaction currency';
            END IF;
            IF NEW.currency <> v_account_currency THEN
                RAISE EXCEPTION 'entry currency does not match account currency';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """


def _hardened_transaction_delete_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION reject_ledger_transaction_delete()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'POSTED' THEN
                IF ledger_backfill_transaction_delete_authorized(OLD.id) THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'posted transaction headers cannot be deleted';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
    """


def _restore_0004_functions() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_ledger_entry_mutation()
        RETURNS trigger AS $$
        DECLARE
            v_status text;
            v_tx_currency varchar(3);
            v_account_currency varchar(3);
        BEGIN
            IF TG_OP = 'UPDATE'
               AND NEW.transaction_id IS DISTINCT FROM OLD.transaction_id THEN
                RAISE EXCEPTION 'ledger entry transaction cannot be changed';
            END IF;

            SELECT status INTO v_status
            FROM ledger_transactions
            WHERE id = OLD.transaction_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'invalid ledger transaction';
            END IF;
            IF v_status = 'POSTED' THEN
                RAISE EXCEPTION 'posted ledger entries are immutable';
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM ledger_transactions WHERE id = NEW.transaction_id
            ) THEN
                RAISE EXCEPTION 'invalid ledger transaction';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM ledger_accounts WHERE id = NEW.account_id
            ) THEN
                RAISE EXCEPTION 'invalid ledger account';
            END IF;

            SELECT currency INTO v_tx_currency
            FROM ledger_transactions WHERE id = NEW.transaction_id;
            SELECT currency INTO v_account_currency
            FROM ledger_accounts WHERE id = NEW.account_id;

            IF NEW.currency <> v_tx_currency THEN
                RAISE EXCEPTION 'entry currency does not match transaction currency';
            END IF;
            IF NEW.currency <> v_account_currency THEN
                RAISE EXCEPTION 'entry currency does not match account currency';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_ledger_transaction_delete()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'POSTED' THEN
                RAISE EXCEPTION 'posted transaction headers cannot be deleted';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def _parse_metadata_uuid(value: object, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid UUID in metadata field {field}: {value!r}") from exc


def _preflight_opening_backfills(
    bind: object,
    markers: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, uuid.UUID]], list[uuid.UUID]]:
    """Validate every migration marker before any downgrade deletion."""
    system_accounts = bind.execute(  # type: ignore[attr-defined]
        text(
            "SELECT id FROM ledger_accounts "
            "WHERE account_type = 'SYSTEM_OPENING_EQUITY' AND currency = 'INR'"
        )
    ).mappings().all()
    if len(system_accounts) != 1:
        raise RuntimeError("expected exactly one SYSTEM_OPENING_EQUITY INR account")
    system_account_id = system_accounts[0]["id"]

    delete_accounts = [
        marker["account_id"] for marker in markers if marker["account_created_by_migration"]
    ]
    plans: list[dict[str, uuid.UUID]] = []

    for marker in markers:
        wallet_id = marker["wallet_id"]
        account_id = marker["account_id"]
        balance = marker["opening_balance"]
        transaction_id = marker["transaction_id"]
        label = f"marker for wallet {wallet_id}"

        if balance < 0:
            raise RuntimeError(f"{label} has negative opening balance {balance}")
        if bind.execute(  # type: ignore[attr-defined]
            text("SELECT 1 FROM wallets WHERE id = :wid"), {"wid": wallet_id}
        ).first() is None:
            raise RuntimeError(f"{label} references a missing wallet")

        account = bind.execute(  # type: ignore[attr-defined]
            text(
                "SELECT account_type, wallet_id, currency FROM ledger_accounts WHERE id = :aid"
            ),
            {"aid": account_id},
        ).mappings().first()
        if account is None:
            raise RuntimeError(f"{label} references a missing ledger account")
        if (
            account["account_type"] != USER_WALLET
            or account["wallet_id"] != wallet_id
            or account["currency"] != INR
        ):
            raise RuntimeError(f"{label} references an invalid USER_WALLET account")

        if balance == 0:
            if transaction_id is not None:
                raise RuntimeError(f"{label} is zero-balance but has a transaction")
            if bind.execute(  # type: ignore[attr-defined]
                text("SELECT 1 FROM ledger_entries WHERE account_id = :aid"), {"aid": account_id}
            ).first() is not None:
                raise RuntimeError(f"{label} zero-balance account has ledger entries")
            continue

        if transaction_id is None:
            raise RuntimeError(f"{label} is positive-balance but has no transaction")
        transaction = bind.execute(  # type: ignore[attr-defined]
            text(
                "SELECT status, kind, amount, currency, metadata "
                "FROM ledger_transactions WHERE id = :tid"
            ),
            {"tid": transaction_id},
        ).mappings().first()
        if transaction is None:
            raise RuntimeError(f"{label} references a missing opening transaction")
        if transaction["status"] != "POSTED":
            raise RuntimeError(f"{label} opening transaction is not POSTED")
        if transaction["kind"] != "OPENING":
            raise RuntimeError(f"{label} opening transaction has wrong kind")
        if transaction["amount"] != balance:
            raise RuntimeError(f"{label} opening transaction amount does not match marker")
        if transaction["currency"] != INR:
            raise RuntimeError(f"{label} opening transaction currency is not INR")

        metadata = transaction["metadata"] or {}
        if not isinstance(metadata, dict):
            raise RuntimeError(f"{label} opening transaction metadata is not an object")
        if metadata.get("migration") != MIGRATION:
            raise RuntimeError(f"{label} opening transaction metadata has wrong migration")

        required_keys = (
            "wallet_id",
            "wallet_account_id",
            "system_account_id",
            "debit_entry_id",
            "credit_entry_id",
            "opening_balance",
        )
        missing = [key for key in required_keys if key not in metadata]
        if missing:
            raise RuntimeError(
                f"{label} opening transaction metadata missing keys: {', '.join(missing)}"
            )

        metadata_wallet_id = _parse_metadata_uuid(metadata["wallet_id"], "wallet_id")
        metadata_account_id = _parse_metadata_uuid(
            metadata["wallet_account_id"], "wallet_account_id"
        )
        metadata_system_id = _parse_metadata_uuid(
            metadata["system_account_id"], "system_account_id"
        )
        debit_id = _parse_metadata_uuid(metadata["debit_entry_id"], "debit_entry_id")
        credit_id = _parse_metadata_uuid(metadata["credit_entry_id"], "credit_entry_id")

        if metadata_wallet_id != wallet_id:
            raise RuntimeError(f"{label} metadata wallet_id does not match marker")
        if metadata_account_id != account_id:
            raise RuntimeError(f"{label} metadata wallet_account_id does not match marker")
        if metadata_system_id != system_account_id:
            raise RuntimeError(f"{label} metadata system_account_id does not match system account")
        if debit_id == credit_id:
            raise RuntimeError(f"{label} debit and credit entry IDs must be distinct")
        if str(metadata["opening_balance"]) != f"{balance:.2f}":
            raise RuntimeError(f"{label} metadata opening_balance does not match marker")

        entries = bind.execute(  # type: ignore[attr-defined]
            text(
                "SELECT id, transaction_id, direction, account_id, amount, currency "
                "FROM ledger_entries WHERE transaction_id = :tid ORDER BY id"
            ),
            {"tid": transaction_id},
        ).mappings().all()
        if len(entries) != 2:
            raise RuntimeError(
                f"{label} opening transaction must have exactly two entries, found {len(entries)}"
            )
        entry_by_id = {entry["id"]: entry for entry in entries}
        if set(entry_by_id) != {debit_id, credit_id}:
            raise RuntimeError(f"{label} opening transaction has unexpected ledger entries")

        for entry in entries:
            if entry["transaction_id"] != transaction_id:
                raise RuntimeError(f"{label} entry belongs to a different transaction")
            if entry["currency"] != INR:
                raise RuntimeError(f"{label} entry currency is not INR")

        debit_entry = entry_by_id[debit_id]
        credit_entry = entry_by_id[credit_id]
        if debit_entry["direction"] != "DEBIT" or debit_entry["account_id"] != system_account_id:
            raise RuntimeError(f"{label} debit entry is not a DEBIT to SYSTEM_OPENING_EQUITY")
        if credit_entry["direction"] != "CREDIT" or credit_entry["account_id"] != account_id:
            raise RuntimeError(f"{label} credit entry is not a CREDIT to the wallet account")
        if debit_entry["amount"] != balance or credit_entry["amount"] != balance:
            raise RuntimeError(f"{label} entry amounts do not match opening balance")

        plans.append(
            {"transaction_id": transaction_id, "debit": debit_id, "credit": credit_id}
        )

    return plans, delete_accounts


def upgrade() -> None:
    bind = op.get_bind()
    _create_marker_table()
    _execute_plpgsql_block(_authorization_functions())
    op.execute(_hardened_entry_mutation_function())
    op.execute(_hardened_transaction_delete_function())

    system_rows = bind.execute(
        text(
            "SELECT account_type, id, currency FROM ledger_accounts "
            "WHERE account_type IN (:opening, :clearing)"
        ),
        {"opening": SYSTEM_OPENING_EQUITY, "clearing": "SYSTEM_CASH_CLEARING"},
    ).mappings().all()
    types = {row["account_type"] for row in system_rows}
    if types != {SYSTEM_OPENING_EQUITY, "SYSTEM_CASH_CLEARING"}:
        raise RuntimeError("required system ledger accounts are missing or duplicated")
    if any(row["currency"] != INR for row in system_rows):
        raise RuntimeError("system ledger accounts must use INR")
    system_account_id = next(
        row["id"] for row in system_rows if row["account_type"] == SYSTEM_OPENING_EQUITY
    )

    wallets = bind.execute(
        text("SELECT id, balance, currency FROM wallets ORDER BY id")
    ).mappings().all()
    _validate_wallet_rows(wallets)

    existing_accounts = bind.execute(
        text(
            "SELECT id, account_type, wallet_id, currency FROM ledger_accounts "
            "WHERE wallet_id IS NOT NULL"
        )
    ).mappings().all()
    account_by_wallet: dict[uuid.UUID, uuid.UUID] = {}
    for row in existing_accounts:
        if row["account_type"] != USER_WALLET or row["currency"] != INR:
            raise RuntimeError(f"conflicting ledger account for wallet {row['wallet_id']}")
        if row["wallet_id"] in account_by_wallet:
            raise RuntimeError(f"duplicate USER_WALLET account for wallet {row['wallet_id']}")
        account_by_wallet[row["wallet_id"]] = row["id"]

    if bind.execute(
        text("SELECT 1 FROM ledger_opening_backfills WHERE migration = :m LIMIT 1"),
        {"m": MIGRATION},
    ).first():
        raise RuntimeError("opening-balance backfill markers already exist")

    for wallet in wallets:
        wallet_id = wallet["id"]
        balance = wallet["balance"]
        ids = _ids(wallet_id)

        account_id = account_by_wallet.get(wallet_id)
        account_created = account_id is None
        if account_id is None:
            bind.execute(
                text(
                    "INSERT INTO ledger_accounts (id, account_type, wallet_id, currency, name) "
                    "VALUES (:id, :type, :wid, :cur, :name)"
                ),
                {
                    "id": ids["account"],
                    "type": USER_WALLET,
                    "wid": wallet_id,
                    "cur": INR,
                    "name": f"Wallet {wallet_id}",
                },
            )
            account_id = ids["account"]
            account_by_wallet[wallet_id] = account_id

        transaction_id = None
        if balance > 0:
            transaction_id = ids["transaction"]
            metadata = {
                "migration": MIGRATION,
                "wallet_id": str(wallet_id),
                "wallet_account_id": str(account_id),
                "system_account_id": str(system_account_id),
                "debit_entry_id": str(ids["debit_entry"]),
                "credit_entry_id": str(ids["credit_entry"]),
                "opening_balance": f"{balance:.2f}",
            }
            bind.execute(
                text(
                    "INSERT INTO ledger_transactions "
                    "(id, kind, amount, currency, status, posted_at, metadata) "
                    "VALUES (:id, 'OPENING', :amount, :cur, 'PENDING', NULL, CAST(:meta AS jsonb))"
                ),
                {
                    "id": transaction_id,
                    "amount": balance,
                    "cur": INR,
                    "meta": json.dumps(metadata),
                },
            )
            for entry_id, direction, target_account in (
                (ids["debit_entry"], "DEBIT", system_account_id),
                (ids["credit_entry"], "CREDIT", account_id),
            ):
                bind.execute(
                    text(
                        "INSERT INTO ledger_entries "
                        "(id, transaction_id, account_id, direction, amount, currency) "
                        "VALUES (:id, :tid, :aid, :dir, :amount, :cur)"
                    ),
                    {
                        "id": entry_id,
                        "tid": transaction_id,
                        "aid": target_account,
                        "dir": direction,
                        "amount": balance,
                        "cur": INR,
                    },
                )
            bind.execute(
                text(
                    "UPDATE ledger_transactions "
                    "SET status = 'POSTED', posted_at = now() WHERE id = :tid"
                ),
                {"tid": transaction_id},
            )

        bind.execute(
            text(
                "INSERT INTO ledger_opening_backfills "
                "(migration, wallet_id, account_id, transaction_id, opening_balance, "
                "account_created_by_migration) "
                "VALUES (:m, :wid, :aid, :tid, :balance, :created)"
            ),
            {
                "m": MIGRATION,
                "wid": wallet_id,
                "aid": account_id,
                "tid": transaction_id,
                "balance": balance,
                "created": account_created,
            },
        )

    bind.execute(text("SET CONSTRAINTS ctrg_assert_posted_transaction_invariants IMMEDIATE"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text(f"SET LOCAL {GUC_NAME} = '{MIGRATION}'"))
    guc_value = bind.scalar(text("SELECT current_setting('app.phase3_2_backfill', true)"))
    if guc_value != MIGRATION:
        raise RuntimeError(f"GUC not set: {guc_value!r}")

    markers = bind.execute(
        text(
            "SELECT wallet_id, account_id, transaction_id, opening_balance, "
            "account_created_by_migration "
            "FROM ledger_opening_backfills WHERE migration = :m ORDER BY wallet_id"
        ),
        {"m": MIGRATION},
    ).mappings().all()
    plans, account_ids = _preflight_opening_backfills(bind, markers)

    for plan in plans:
        for key in ("debit", "credit"):
            bind.execute(
                text("DELETE FROM ledger_entries WHERE id = :eid"), {"eid": plan[key]}
            )
        bind.execute(
            text("DELETE FROM ledger_transactions WHERE id = :tid"),
            {"tid": plan["transaction_id"]},
        )

    bind.execute(
        text("DELETE FROM ledger_opening_backfills WHERE migration = :m"), {"m": MIGRATION}
    )
    for account_id in account_ids:
        bind.execute(
            text("DELETE FROM ledger_accounts WHERE id = :aid"), {"aid": account_id}
        )

    _restore_0004_functions()
    op.execute(
        "DROP FUNCTION IF EXISTS ledger_backfill_transaction_delete_authorized(uuid) CASCADE;"
    )
    op.execute("DROP FUNCTION IF EXISTS ledger_backfill_entry_delete_authorized(uuid) CASCADE;")
    op.drop_index("ix_ledger_opening_backfills_account", table_name="ledger_opening_backfills")
    op.drop_index(
        "ux_ledger_opening_backfills_transaction", table_name="ledger_opening_backfills"
    )
    op.drop_table("ledger_opening_backfills")
