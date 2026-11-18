"""Test bootstrap.

Environment must be configured *before* ``app`` is imported because settings
(and the SQLAlchemy engine) are created at import time.

Tests are split into two groups:

* ``tests/unit`` â€” run anywhere, no database required.
* ``tests/integration`` â€” require PostgreSQL (``auth_db_test``). The DSN comes
  from ``AUTH_TEST_DATABASE_URL``; it is never defaulted to the runtime database.

Example::

    AUTH_TEST_DATABASE_URL="postgresql+asyncpg://auth_svc:<password>@localhost:5433/auth_db_test" \\
        uv run pytest
"""

from __future__ import annotations

import os

# A cheap bcrypt cost keeps the suite fast. Production default is 12 and is
# asserted separately in the config tests.
os.environ.setdefault("BCRYPT_COST", "4")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("JWT_SECRET", "test-secret-0123456789abcdef0123456789abcdef")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("JWT_ISSUER", "virtual-wallet-auth")
os.environ.setdefault("ACCESS_TOKEN_TTL_MINUTES", "30")

# Unit tests never connect. Integration tests replace this with the real test
# DSN (see tests/integration/conftest.py) or exit with instructions.
os.environ.setdefault(
    "AUTH_DATABASE_URL",
    os.environ.get(
        "AUTH_TEST_DATABASE_URL",
        "postgresql+asyncpg://auth_svc:unset@127.0.0.1:1/auth_db_test",
    ),
)

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.main import app


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """ASGI client with no database override.

    Safe for validation-only tests: the session dependency opens a session but
    does not connect until a query runs.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://auth") as http_client:
        yield http_client


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()
