"""Money-safety rules for amount and wallet schemas."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.wallet import MAX_OPERATION_AMOUNT, Wallet
from app.schemas.wallet import AmountRequest, WalletOut


@pytest.mark.parametrize("raw", ["500.00", "500", 500, Decimal("500.00")])
def test_valid_amounts_are_parsed_as_decimal(raw: object) -> None:
    assert AmountRequest(amount=raw).amount == Decimal("500.00")  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["0", "-1.00", "0.001", "nan", "abc", 12.5, True])
def test_invalid_amounts_are_rejected(raw: object) -> None:
    with pytest.raises(ValidationError):
        AmountRequest(amount=raw)  # type: ignore[arg-type]


def test_amount_limit_is_enforced() -> None:
    assert AmountRequest(amount=str(MAX_OPERATION_AMOUNT)).amount == MAX_OPERATION_AMOUNT  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AmountRequest(amount=str(MAX_OPERATION_AMOUNT + Decimal("0.01")))  # type: ignore[arg-type]


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AmountRequest(amount="10.00", user_id=str(uuid.uuid4()))  # type: ignore[arg-type, call-arg]


def test_balance_is_serialized_as_a_fixed_two_decimal_string() -> None:
    user_id = uuid.uuid4()
    wallet = Wallet(user_id=user_id, balance=Decimal("5.5"), currency="INR")
    wallet.id = uuid.uuid4()
    wallet.created_at = datetime(2026, 11, 23, tzinfo=UTC)
    wallet.updated_at = datetime(2026, 11, 23, tzinfo=UTC)

    body = WalletOut.model_validate(wallet).model_dump(mode="json")

    assert body["balance"] == "5.50"
    assert isinstance(body["balance"], str)
