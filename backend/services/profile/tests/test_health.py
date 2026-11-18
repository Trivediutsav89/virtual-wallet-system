"""Profile Service tests.

The service is health-only in Stage 1, so the suite covers liveness and the
readiness failure path. It intentionally needs no live Postgres: the DSN points
at a closed port, which must yield a clean 503 rather than a crash.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
os.environ.setdefault(
    "PROFILE_DATABASE_URL",
    "postgresql+asyncpg://profile_svc:pw@127.0.0.1:1/profile_db_test",
)

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://profile") as http_client:
            yield http_client


async def test_healthz_is_liveness_only(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "profile-service", "env": "test"}


async def test_readyz_reports_database_failure(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "unavailable"


async def test_unknown_route_uses_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/profile/me")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


async def test_no_business_endpoints_are_exposed(client: AsyncClient) -> None:
    """Guard against scope creep: Stage 1 exposes health checks only."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = set(response.json()["paths"])
    assert paths == {"/healthz", "/readyz"}
