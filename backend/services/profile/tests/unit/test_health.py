"""Liveness, readiness, error envelope and API surface."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_is_liveness_only(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "profile-service", "env": "test"}


async def test_readyz_reports_database_failure(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "unavailable"}}


async def test_unknown_route_uses_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


async def test_openapi_exposes_only_the_approved_endpoints(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert set(response.json()["paths"]) == {"/healthz", "/readyz", "/me"}
