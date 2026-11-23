"""create wallets

Revision ID: 0002_create_wallets
Revises: 0001_baseline
Create Date: 2026-11-23

Notes
-----
* ``balance`` is ``NUMERIC(18,2)`` — never a floating-point column.
* A CHECK constraint makes a negative balance impossible at the database level,
  even if application logic is bypassed.
* Currency is fixed to INR for the Day 2 MVP (no FX, no multi-currency wallets).
* Day 3 may add an immutable ledger; see ADR-0005.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_create_wallets"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "balance",
            sa.Numeric(18, 2),
            server_default=sa.text("0.00"),
            nullable=False,
        ),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("balance >= 0", name="ck_wallets_balance_non_negative"),
        sa.CheckConstraint("currency = 'INR'", name="ck_wallets_currency_inr"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallets")),
    )
    op.create_index("ux_wallets_user_id", "wallets", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_wallets_user_id", table_name="wallets")
    op.drop_table("wallets")
