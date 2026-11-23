"""Money-safe request/response schemas.

Amounts are transported as strings or integers and converted to ``Decimal``.
Python floats are rejected outright: binary floating point cannot represent
decimal currency values exactly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from app.models.wallet import MAX_OPERATION_AMOUNT


class AmountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal

    @field_validator("amount", mode="before")
    @classmethod
    def _parse_decimal(cls, value: object) -> object:
        if isinstance(value, (bool, float)):
            raise ValueError("Amount must be a decimal string or integer, not a float")
        if not isinstance(value, (str, int, Decimal)):
            return value
        try:
            parsed = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Amount must be a valid decimal value") from exc
        if not parsed.is_finite():
            raise ValueError("Amount must be a finite decimal value")
        return parsed

    @field_validator("amount")
    @classmethod
    def _validate_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("Amount must be greater than zero")
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise ValueError("Amount must have at most 2 decimal places")
        if value > MAX_OPERATION_AMOUNT:
            raise ValueError(f"Amount must not exceed {MAX_OPERATION_AMOUNT}")
        return value


class WalletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    balance: Decimal
    currency: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("balance")
    def _serialize_balance(self, value: Decimal) -> str:
        """Fixed two-decimal string; never a JSON floating-point number."""
        return f"{value:.2f}"


__all__ = ["AmountRequest", "WalletOut"]
