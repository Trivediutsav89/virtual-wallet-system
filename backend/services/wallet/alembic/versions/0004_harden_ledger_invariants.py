"""harden ledger invariants

Revision ID: 0004_harden_ledger_invariants
Revises: 0003_create_ledger_schema
Create Date: 2026-11-24

Corrective migration. 0003 is already applied to existing databases, so the
fix is installed here rather than by editing the applied revision.

Changes:
* entry UPDATE/DELETE now locks and re-checks the parent transaction row;
* changing ``ledger_entries.transaction_id`` is prohibited outright (the
  simplest safe rule), so an entry can never be reparented into a posted or
  otherwise invariant-checked transaction;
* entry currency/account consistency is re-validated on UPDATE as well as INSERT;
* ``ledger_accounts`` identity fields are frozen and system accounts cannot be
  deleted.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_harden_ledger_invariants"
down_revision: str | None = "0003_create_ledger_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _entry_mutation_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION reject_ledger_entry_mutation()
        RETURNS trigger AS $$
        DECLARE
            v_status text;
            v_tx_currency varchar(3);
            v_account_currency varchar(3);
        BEGIN
            -- Simplest safe rule: an entry is permanently bound to the
            -- transaction it was created in. Reparenting is never allowed.
            IF TG_OP = 'UPDATE'
               AND NEW.transaction_id IS DISTINCT FROM OLD.transaction_id THEN
                RAISE EXCEPTION 'ledger entry transaction cannot be changed';
            END IF;

            -- Lock the parent row first, then re-check its status under the
            -- lock. This serialises entry mutation against PENDING -> POSTED.
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

            -- Re-validate currency consistency for PENDING entries too.
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


def _new_entry_insert_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION validate_new_ledger_entry()
        RETURNS trigger AS $$
        DECLARE
            v_status text;
            v_tx_currency varchar(3);
            v_account_currency varchar(3);
        BEGIN
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


def _old_entry_mutation_function() -> str:
    """Exact pre-0004 implementation, restored by downgrade()."""
    return """
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


def _old_entry_insert_function() -> str:
    """Exact pre-0004 implementation, restored by downgrade()."""
    return """
        CREATE OR REPLACE FUNCTION validate_new_ledger_entry()
        RETURNS trigger AS $$
        DECLARE
            v_status text;
            v_tx_currency varchar(3);
            v_account_currency varchar(3);
        BEGIN
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


def _account_identity_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION protect_ledger_account_identity()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.account_type <> 'USER_WALLET' THEN
                    RAISE EXCEPTION 'system ledger accounts cannot be deleted';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.account_type IS DISTINCT FROM NEW.account_type
               OR OLD.wallet_id IS DISTINCT FROM NEW.wallet_id
               OR OLD.currency IS DISTINCT FROM NEW.currency THEN
                RAISE EXCEPTION 'ledger account identity fields are immutable';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """


def _entry_triggers() -> None:
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


def upgrade() -> None:
    # Replace the two entry triggers with hardened versions.
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_immutable ON ledger_entries;")
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_posted_guard ON ledger_entries;")
    op.execute(_entry_mutation_function())
    op.execute(_new_entry_insert_function())
    _entry_triggers()

    # Freeze account identity and protect system accounts.
    op.execute(_account_identity_function())
    op.execute(
        """
        CREATE TRIGGER trg_ledger_accounts_identity
        BEFORE UPDATE OR DELETE ON ledger_accounts
        FOR EACH ROW EXECUTE FUNCTION protect_ledger_account_identity();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_accounts_identity ON ledger_accounts;")
    op.execute("DROP FUNCTION IF EXISTS protect_ledger_account_identity() CASCADE;")

    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_immutable ON ledger_entries;")
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_posted_guard ON ledger_entries;")
    op.execute(_old_entry_mutation_function())
    op.execute(_old_entry_insert_function())
    _entry_triggers()
