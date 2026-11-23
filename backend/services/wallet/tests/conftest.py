"""Test bootstrap for the Wallet Service.

The app-level DSN is deliberately unreachable so unit tests can assert that
``/readyz`` reports a dead database. Integration tests build their own engine
from ``WALLET_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ["WALLET_DATABASE_URL"] = (
    "postgresql+asyncpg://wallet_svc:unset@127.0.0.1:1/wallet_db_test"
)

from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://wallet") as http_client:
        yield http_client


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()
