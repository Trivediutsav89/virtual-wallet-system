"""Wallet model.

The Day 2 balance is a stored ``NUMERIC(18,2)`` value guarded by a database
CHECK constraint and conditional updates. Day 3 may introduce an immutable
ledger and reconcile this column; see ADR-0005.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Index, Numeric, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

CURRENCY_INR = "INR"
MAX_BALANCE = Decimal("9999999999999999.99")  # NUMERIC(18,2)
MAX_OPERATION_AMOUNT = Decimal("1000000.00")


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_wallets_balance_non_negative"),
        CheckConstraint("currency = 'INR'", name="ck_wallets_currency_inr"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, server_default=text("0.00")
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'INR'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


Index("ux_wallets_user_id", Wallet.user_id, unique=True)


__all__ = ["CURRENCY_INR", "MAX_BALANCE", "MAX_OPERATION_AMOUNT", "Wallet"]
