"""Immutable double-entry ledger models.

``wallets.balance`` remains a cached projection; the ledger is the source of
truth. All invariants are enforced in PostgreSQL by migration 0003, so these
models only describe the schema for ORM use and future autogenerate checks.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

MAX_OPERATION_AMOUNT = Decimal("1000000.00")
CURRENCY_INR = "INR"


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = (
        CheckConstraint(
            "(account_type = 'USER_WALLET' AND wallet_id IS NOT NULL) OR "
            "(account_type <> 'USER_WALLET' AND wallet_id IS NULL)",
            name="ck_ledger_accounts_wallet_presence",
        ),
        CheckConstraint("currency = 'INR'", name="ck_ledger_accounts_currency_inr"),
        Index(
            "ux_ledger_accounts_wallet",
            "wallet_id",
            unique=True,
            postgresql_where=text("account_type = 'USER_WALLET'"),
        ),
        Index(
            "ux_ledger_accounts_system_type",
            "account_type",
            unique=True,
            postgresql_where=text("account_type <> 'USER_WALLET'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    account_type: Mapped[str] = mapped_column(String(40), nullable=False)
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=True
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'INR'")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_transactions_amount_positive"),
        CheckConstraint(
            "status IN ('PENDING', 'POSTED')",
            name="ck_ledger_transactions_status_valid",
        ),
        CheckConstraint(
            "status <> 'POSTED' OR posted_at IS NOT NULL",
            name="ck_ledger_transactions_posted_at_required",
        ),
        CheckConstraint("currency = 'INR'", name="ck_ledger_transactions_currency_inr"),
        CheckConstraint(
            "(kind <> 'OPENING' AND amount <= 1000000.00) OR kind = 'OPENING'",
            name="ck_ledger_transactions_amount_cap",
        ),
        Index("ix_ledger_transactions_kind_created", "kind", "created_at", "id"),
        Index("ix_ledger_transactions_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'INR'")
    )
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'PENDING'")
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),
        CheckConstraint(
            "direction IN ('DEBIT', 'CREDIT')",
            name="ck_ledger_entries_direction_valid",
        ),
        CheckConstraint("currency = 'INR'", name="ck_ledger_entries_currency_inr"),
        Index("ix_ledger_entries_transaction", "transaction_id"),
        Index("ix_ledger_entries_account_created", "account_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'INR'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
        CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED')",
            name="ck_idempotency_status_valid",
        ),
        Index("ix_idempotency_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'IN_PROGRESS'")
    )
    response_code: Mapped[int | None] = mapped_column(nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = [
    "CURRENCY_INR",
    "MAX_OPERATION_AMOUNT",
    "IdempotencyRecord",
    "LedgerAccount",
    "LedgerEntry",
    "LedgerTransaction",
]
