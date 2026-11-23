"""HTTP contract of the Wallet Service with the service layer faked."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.routes import wallet as wallet_routes
from app.db import get_session
from app.errors import auth_required, insufficient_funds
from app.main import app
from app.models.wallet import Wallet

USER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
HEADERS = {"X-User-Id": str(USER_ID)}


def _make_wallet(balance: str = "0.00") -> Wallet:
    wallet = Wallet(user_id=USER_ID, balance=Decimal(balance), currency="INR")
    wallet.id = uuid.uuid4()
    wallet.created_at = datetime(2026, 11, 23, tzinfo=UTC)
    wallet.updated_at = datetime(2026, 11, 23, tzinfo=UTC)
    return wallet


class FakeWalletService:
    error: Exception | None = None
    calls: ClassVar[list[tuple[str, Any]]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def get_or_create(self, user_id: uuid.UUID) -> Wallet:
        self.calls.append(("get_or_create", user_id))
        if self.error:
            raise self.error
        return _make_wallet()

    async def deposit(self, user_id: uuid.UUID, amount: Decimal) -> Wallet:
        self.calls.append(("deposit", (user_id, amount)))
        if self.error:
            raise self.error
        return _make_wallet(str(Decimal("0.00") + amount))

    async def withdraw(self, user_id: uuid.UUID, amount: Decimal) -> Wallet:
        self.calls.append(("withdraw", (user_id, amount)))
        if self.error:
            raise self.error
        return _make_wallet("400.00")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async def fake_session() -> AsyncIterator[Any]:
        yield object()

    app.dependency_overrides[get_session] = fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://wallet") as http_client:
        yield http_client


@pytest.fixture(autouse=True)
def fake_service(monkeypatch: pytest.MonkeyPatch) -> type[FakeWalletService]:
    FakeWalletService.error = None
    FakeWalletService.calls = []
    monkeypatch.setattr(wallet_routes, "WalletService", FakeWalletService)
    return FakeWalletService


async def test_get_me_returns_zero_balance_inr(client: AsyncClient) -> None:
    response = await client.get("/me", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["balance"] == "0.00"
    assert body["currency"] == "INR"
    assert body["user_id"] == str(USER_ID)


async def test_get_me_requires_identity(client: AsyncClient) -> None:
    response = await client.get("/me")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


async def test_deposit_returns_updated_balance(client: AsyncClient) -> None:
    response = await client.post(
        "/deposit", json={"amount": "500.00"}, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["balance"] == "500.00"


async def test_withdraw_returns_updated_balance(client: AsyncClient) -> None:
    response = await client.post(
        "/withdraw", json={"amount": "100.00"}, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["balance"] == "400.00"


async def test_deposit_rejects_float(client: AsyncClient) -> None:
    response = await client.post("/deposit", json={"amount": 10.5}, headers=HEADERS)
    assert response.status_code == 422
    assert all("input" not in error for error in response.json()["detail"])


async def test_deposit_rejects_user_id_in_body(client: AsyncClient) -> None:
    spoof = str(uuid.uuid4())
    response = await client.post(
        "/deposit", json={"amount": "10.00", "user_id": spoof}, headers=HEADERS
    )
    assert response.status_code == 422
    assert spoof not in response.text


async def test_insufficient_funds_envelope(
    client: AsyncClient, fake_service: type[FakeWalletService]
) -> None:
    fake_service.error = insufficient_funds()
    response = await client.post(
        "/withdraw", json={"amount": "9999.00"}, headers=HEADERS
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "INSUFFICIENT_FUNDS",
            "message": "Insufficient balance for this withdrawal.",
        }
    }


async def test_service_error_is_not_leaked(
    client: AsyncClient, fake_service: type[FakeWalletService]
) -> None:
    fake_service.error = auth_required("Malformed identity header.")
    response = await client.get("/me", headers=HEADERS)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
