"""PostgreSQL-backed fixtures for the Wallet Service.

Skips cleanly without ``WALLET_TEST_DATABASE_URL``; applies the real migration
chain once; isolates every test in a rolled-back outer transaction.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app

SERVICE_ROOT = Path(__file__).resolve().parents[2]
CONNECT_TIMEOUT_SECONDS = 5


def _redact(dsn: str) -> str:
    try:
        return make_url(dsn).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable DSN>"


def _can_connect(dsn: str) -> bool:
    async def check() -> None:
        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(asyncio.wait_for(check(), timeout=CONNECT_TIMEOUT_SECONDS))
    except (TimeoutError, OSError):
        return False
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def test_dsn() -> str:
    dsn = os.environ.get("WALLET_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "WALLET_TEST_DATABASE_URL is not set; PostgreSQL-backed tests skipped. "
            'Example: WALLET_TEST_DATABASE_URL="postgresql+asyncpg://wallet_svc:pw@'
            'localhost:5433/wallet_db_test"'
        )
    if not dsn.startswith("postgresql+asyncpg://"):
        pytest.fail("WALLET_TEST_DATABASE_URL must use the postgresql+asyncpg:// driver")
    if not _can_connect(dsn):
        pytest.skip(f"PostgreSQL is not reachable at {_redact(dsn)}")
    return dsn


@pytest.fixture(scope="session")
def migrated(test_dsn: str) -> None:
    environment = {**os.environ, "WALLET_DATABASE_URL": test_dsn}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=SERVICE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed against the test database:\n"
            f"{result.stdout}\n{result.stderr}"
        )


@pytest.fixture
async def db_session(test_dsn: str, migrated: None) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            maker = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                autoflush=False,
                join_transaction_mode="create_savepoint",
            )
            async with maker() as session:
                try:
                    yield session
                finally:
                    await session.close()
            await outer.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def db_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://wallet") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
