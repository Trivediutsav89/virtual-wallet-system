"""Test bootstrap for the Profile Service.

Environment is configured before ``app`` is imported because settings and the
SQLAlchemy engine are created at import time. The app-level DSN is deliberately
unreachable so unit tests can assert that ``/readyz`` reports a dead database,
even when the integration DSN is configured. Integration tests build their own
engine from ``PROFILE_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ["PROFILE_DATABASE_URL"] = (
    "postgresql+asyncpg://profile_svc:unset@127.0.0.1:1/profile_db_test"
)

from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """ASGI client with no repository or session override."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://profile") as http_client:
        yield http_client


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()
