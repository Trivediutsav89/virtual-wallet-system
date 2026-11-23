"""Wallet business logic with a fake session; no PostgreSQL required."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.wallet import Wallet
from app.repositories.wallet_repository import WalletRepository
from app.services.wallet_service import WalletService

USER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _instance: Any) -> None:
        self.refreshes += 1


def _make_wallet(balance: str = "0.00") -> Wallet:
    wallet = Wallet(user_id=USER_ID, balance=Decimal(balance), currency="INR")
    wallet.id = uuid.uuid4()
    wallet.created_at = datetime(2026, 11, 23, tzinfo=UTC)
    wallet.updated_at = datetime(2026, 11, 23, tzinfo=UTC)
    return wallet


def _patch_repository(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wallet: Wallet | None = None,
    deposit: Wallet | None = None,
    withdraw: Wallet | None = None,
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"create": [], "deposit": [], "withdraw": []}

    async def fake_get_or_create(
        self: WalletRepository, user_id: uuid.UUID
    ) -> Wallet | None:
        calls["create"].append(user_id)
        return wallet if wallet is not None else _make_wallet()

    async def fake_deposit(
        self: WalletRepository, user_id: uuid.UUID, amount: Decimal
    ) -> Wallet | None:
        calls["deposit"].append((user_id, amount))
        return deposit

    async def fake_withdraw(
        self: WalletRepository, user_id: uuid.UUID, amount: Decimal
    ) -> Wallet | None:
        calls["withdraw"].append((user_id, amount))
        return withdraw

    monkeypatch.setattr(WalletRepository, "get_or_create", fake_get_or_create)
    monkeypatch.setattr(WalletRepository, "deposit", fake_deposit)
    monkeypatch.setattr(WalletRepository, "withdraw", fake_withdraw)
    return calls


async def test_get_or_create_uses_trusted_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_repository(monkeypatch)
    session = FakeSession()

    await WalletService(cast(AsyncSession, session)).get_or_create(USER_ID)

    assert calls["create"] == [USER_ID]
    assert session.commits == 1


async def test_deposit_is_decimal_and_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_repository(monkeypatch, deposit=_make_wallet("500.00"))
    session = FakeSession()

    result = await WalletService(cast(AsyncSession, session)).deposit(
        USER_ID, Decimal("500.00")
    )

    assert result.balance == Decimal("500.00")
    assert calls["deposit"] == [(USER_ID, Decimal("500.00"))]
    assert session.commits >= 2


async def test_withdraw_is_decimal_and_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_repository(monkeypatch, withdraw=_make_wallet("400.00"))
    session = FakeSession()

    result = await WalletService(cast(AsyncSession, session)).withdraw(
        USER_ID, Decimal("100.00")
    )

    assert result.balance == Decimal("400.00")
    assert calls["withdraw"] == [(USER_ID, Decimal("100.00"))]
    assert session.commits >= 2


async def test_insufficient_funds_raises_409_without_extra_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_repository(monkeypatch, withdraw=None)
    session = FakeSession()

    with pytest.raises(AppError) as excinfo:
        await WalletService(cast(AsyncSession, session)).withdraw(
            USER_ID, Decimal("999.99")
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "INSUFFICIENT_FUNDS"
    assert len(calls["withdraw"]) == 1
    assert session.commits == 1  # only the lazy-create commit


async def test_deposit_failure_does_not_report_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_repository(monkeypatch, deposit=None)
    session = FakeSession()

    with pytest.raises(AppError) as excinfo:
        await WalletService(cast(AsyncSession, session)).deposit(
            USER_ID, Decimal("10.00")
        )

    assert excinfo.value.code == "INTERNAL_ERROR"
    assert session.commits == 1  # only the lazy-create commit
