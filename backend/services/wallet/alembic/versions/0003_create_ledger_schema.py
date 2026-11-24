"""create ledger schema

Revision ID: 0003_create_ledger_schema
Revises: 0002_create_wallets
Create Date: 2026-11-24

Phase 3.1: creates the immutable double-entry ledger tables, database-level
invariants, triggers and system accounts. It intentionally does NOT backfill
opening balances (phase 3.2) and does not touch existing ``wallets`` data
beyond adding an explicit upper-bound check.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_create_ledger_schema"
down_revision: str | None = "0002_create_wallets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_functions() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_ledger_entry_mutation()
        RETURNS trigger AS $$
        DECLARE
            v_status text;
        BEGIN
            SELECT status INTO v_status
            FROM ledger_transactions
            WHERE id = OLD.transaction_id;

            IF v_status = 'POSTED' THEN
                RAISE EXCEPTION 'posted ledger entries are immutable';
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_new_ledger_entry()
        RETURNS trigger AS $$
        DECLARE
            v_status text;
            v_tx_currency varchar(3);
            v_account_currency varchar(3);
        BEGIN
            -- Locks the parent transaction row. This serialises concurrent
            -- entry insertion against PENDING -> POSTED posting.
            SELECT status, currency INTO v_status, v_tx_currency
            FROM ledger_transactions
            WHERE id = NEW.transaction_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'invalid ledger transaction';
            END IF;
            IF v_status <> 'PENDING' THEN
                RAISE EXCEPTION 'cannot insert entry into posted transaction';
            END IF;

            SELECT currency INTO v_account_currency
            FROM ledger_accounts
            WHERE id = NEW.account_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'invalid ledger account';
            END IF;

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
        CREATE OR REPLACE FUNCTION enforce_ledger_transaction_state()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'PENDING' OR NEW.posted_at IS NOT NULL THEN
                    RAISE EXCEPTION 'transactions must be created as PENDING';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'POSTED' THEN
                RAISE EXCEPTION 'posted transaction headers are immutable';
            END IF;

            IF NEW.status = 'PENDING' THEN
                IF NEW.posted_at IS NOT NULL THEN
                    RAISE EXCEPTION 'pending transactions cannot have posted_at';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'PENDING' AND NEW.status = 'POSTED' THEN
                IF NEW.posted_at IS NULL THEN
                    RAISE EXCEPTION 'posted_at is required when posting a transaction';
                END IF;
                RETURN NEW;
            END IF;

            RAISE EXCEPTION 'invalid transaction status transition';
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assert_posted_transaction_invariants()
        RETURNS trigger AS $$
        DECLARE
            v_count integer;
            v_debit numeric(18, 2);
            v_credit numeric(18, 2);
        BEGIN
            IF NEW.status <> 'POSTED' THEN
                RETURN NULL;
            END IF;

            SELECT
                count(*),
                COALESCE(SUM(CASE WHEN direction = 'DEBIT' THEN amount ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN direction = 'CREDIT' THEN amount ELSE 0 END), 0)
            INTO v_count, v_debit, v_credit
            FROM ledger_entries
            WHERE transaction_id = NEW.id;

            IF v_count < 2 THEN
                RAISE EXCEPTION 'posted transaction must have at least two entries';
            END IF;
            IF v_debit <> v_credit THEN
                RAISE EXCEPTION 'posted transaction is not balanced';
            END IF;
            IF v_debit <> NEW.amount OR v_credit <> NEW.amount THEN
                RAISE EXCEPTION 'posted transaction amount does not match entries';
            END IF;

            IF EXISTS (
                SELECT 1 FROM ledger_entries e
                WHERE e.transaction_id = NEW.id AND e.currency <> NEW.currency
            ) THEN
                RAISE EXCEPTION 'entry currency does not match transaction currency';
            END IF;
            IF EXISTS (
                SELECT 1 FROM ledger_entries e
                JOIN ledger_accounts la ON la.id = e.account_id
                WHERE e.transaction_id = NEW.id AND la.currency <> NEW.currency
            ) THEN
                RAISE EXCEPTION 'entry currency does not match account currency';
            END IF;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def _create_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_ledger_entries_immutable
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION reject_ledger_entry_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_entries_posted_guard
        BEFORE INSERT ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION validate_new_ledger_entry();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_transactions_state
        BEFORE INSERT OR UPDATE ON ledger_transactions
        FOR EACH ROW EXECUTE FUNCTION enforce_ledger_transaction_state();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_transactions_no_delete
        BEFORE DELETE ON ledger_transactions
        FOR EACH ROW EXECUTE FUNCTION reject_ledger_transaction_delete();
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_assert_posted_transaction_invariants
        AFTER UPDATE OF status ON ledger_transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION assert_posted_transaction_invariants();
        """
    )


def _seed_system_accounts() -> None:
    op.execute(
        """
        INSERT INTO ledger_accounts (account_type, currency, name)
        VALUES
            ('SYSTEM_CASH_CLEARING', 'INR', 'System cash clearing'),
            ('SYSTEM_OPENING_EQUITY', 'INR', 'System opening equity')
        ON CONFLICT (account_type) WHERE account_type <> 'USER_WALLET' DO NOTHING;
        """
    )


def upgrade() -> None:
    # Explicit upper bound; NUMERIC(18,2) already imposes it, but making it a
    # named CHECK allows clean downgrade and clear reconciliation errors.
    op.execute(
        """
        ALTER TABLE wallets
        ADD CONSTRAINT ck_wallets_balance_upper
        CHECK (balance <= 9999999999999999.99);
        """
    )

    op.create_table(
        "ledger_accounts",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("account_type", sa.String(length=40), nullable=False),
        sa.Column(
            "wallet_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("wallets.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default=sa.text("'INR'"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(account_type = 'USER_WALLET' AND wallet_id IS NOT NULL) OR "
            "(account_type <> 'USER_WALLET' AND wallet_id IS NULL)",
            name="ck_ledger_accounts_wallet_presence",
        ),
        sa.CheckConstraint("currency = 'INR'", name="ck_ledger_accounts_currency_inr"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_accounts")),
    )
    op.create_index(
        "ux_ledger_accounts_wallet",
        "ledger_accounts",
        ["wallet_id"],
        unique=True,
        postgresql_where=sa.text("account_type = 'USER_WALLET'"),
    )
    op.create_index(
        "ux_ledger_accounts_system_type",
        "ledger_accounts",
        ["account_type"],
        unique=True,
        postgresql_where=sa.text("account_type <> 'USER_WALLET'"),
    )

    op.create_table(
        "ledger_transactions",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default=sa.text("'INR'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=10),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_ledger_transactions_amount_positive"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'POSTED')",
            name="ck_ledger_transactions_status_valid",
        ),
        sa.CheckConstraint(
            "status <> 'POSTED' OR posted_at IS NOT NULL",
            name="ck_ledger_transactions_posted_at_required",
        ),
        sa.CheckConstraint(
            "currency = 'INR'", name="ck_ledger_transactions_currency_inr"
        ),
        sa.CheckConstraint(
            "(kind <> 'OPENING' AND amount <= 1000000.00) OR kind = 'OPENING'",
            name="ck_ledger_transactions_amount_cap",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_transactions")),
    )
    op.create_index(
        "ix_ledger_transactions_kind_created",
        "ledger_transactions",
        ["kind", "created_at", "id"],
    )
    op.create_index(
        "ix_ledger_transactions_status_created",
        "ledger_transactions",
        ["status", "created_at"],
    )

    op.create_table(
        "ledger_entries",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "transaction_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("ledger_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(length=6), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default=sa.text("'INR'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),
        sa.CheckConstraint(
            "direction IN ('DEBIT', 'CREDIT')", name="ck_ledger_entries_direction_valid"
        ),
        sa.CheckConstraint("currency = 'INR'", name="ck_ledger_entries_currency_inr"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
    )
    op.create_index("ix_ledger_entries_transaction", "ledger_entries", ["transaction_id"])
    op.create_index(
        "ix_ledger_entries_account_created",
        "ledger_entries",
        ["account_id", "created_at", "id"],
    )

    op.create_table(
        "idempotency_records",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=12),
            server_default=sa.text("'IN_PROGRESS'"),
            nullable=False,
        ),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column(
            "transaction_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED')",
            name="ck_idempotency_status_valid",
        ),
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
    )
    op.create_index(
        "ix_idempotency_status_created",
        "idempotency_records",
        ["status", "created_at"],
    )

    _create_functions()
    _create_triggers()
    _seed_system_accounts()


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS ctrg_assert_posted_transaction_invariants "
        "ON ledger_transactions;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ledger_transactions_no_delete ON ledger_transactions;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ledger_transactions_state ON ledger_transactions;"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_posted_guard ON ledger_entries;")
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_immutable ON ledger_entries;")

    for function in (
        "assert_posted_transaction_invariants",
        "reject_ledger_transaction_delete",
        "enforce_ledger_transaction_state",
        "validate_new_ledger_entry",
        "reject_ledger_entry_mutation",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}() CASCADE;")

    op.drop_table("idempotency_records")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("ledger_accounts")

    op.execute("ALTER TABLE wallets DROP CONSTRAINT IF EXISTS ck_wallets_balance_upper;")
