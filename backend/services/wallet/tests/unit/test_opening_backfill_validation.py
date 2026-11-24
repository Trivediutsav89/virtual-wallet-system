"""Pure validation tests for the opening-balance backfill migration."""

from __future__ import annotations

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0005_backfill_opening_balances.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("opening_backfill_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


def _row(balance: str = "100.00", currency: str = "INR") -> dict[str, object]:
    return {"id": uuid.uuid4(), "balance": Decimal(balance), "currency": currency}


def test_valid_wallet_rows_are_accepted() -> None:
    migration._validate_wallet_rows([_row("0.00"), _row("2500000.00")])


def test_unsupported_currency_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="unsupported currency"):
        migration._validate_wallet_rows([_row(currency="USD")])


def test_negative_balance_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="negative balance"):
        migration._validate_wallet_rows([_row("-0.01")])


def test_balance_above_numeric_limit_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="exceeds NUMERIC"):
        migration._validate_wallet_rows([_row("10000000000000000.00")])


def test_invalid_scale_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="invalid scale"):
        migration._validate_wallet_rows([_row("1.001")])


def test_duplicate_wallet_id_is_rejected() -> None:
    shared = uuid.uuid4()
    rows: list[dict[str, object]] = [
        {"id": shared, "balance": Decimal("1.00"), "currency": "INR"},
        {"id": shared, "balance": Decimal("2.00"), "currency": "INR"},
    ]
    with pytest.raises(RuntimeError, match="duplicate wallet id"):
        migration._validate_wallet_rows(rows)


def test_deterministic_ids_are_stable_and_distinct() -> None:
    wallet_id = uuid.uuid4()
    first = migration._ids(wallet_id)
    second = migration._ids(wallet_id)
    assert first == second
    assert len(set(first.values())) == 4
    other = migration._ids(uuid.uuid4())
    assert set(first.values()).isdisjoint(set(other.values()))
