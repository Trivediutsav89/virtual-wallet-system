"""Application wiring: health checks, request ids, error envelopes, OpenAPI scope."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_is_liveness_only(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "auth-service", "env": "test"}


async def test_readyz_reports_database_failure(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "unavailable"}}


async def test_valid_client_request_id_is_kept(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-Id": "trace-123"})
    assert response.headers["x-request-id"] == "trace-123"


async def test_generated_request_id_is_used_when_absent(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    request_id = response.headers["x-request-id"]

    assert len(request_id) == 32
    assert all(character in "0123456789abcdef" for character in request_id)


async def test_malformed_request_id_is_replaced(client: AsyncClient) -> None:
    # Used for log injection / header smuggling if forwarded verbatim.
    response = await client.get("/healthz", headers={"X-Request-Id": 'bad"\r\nid'})
    request_id = response.headers["x-request-id"]

    assert request_id != 'bad"\r\nid'
    assert len(request_id) == 32


async def test_error_responses_also_carry_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.headers["x-request-id"]


async def test_openapi_exposes_only_the_approved_endpoints(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = set(response.json()["paths"])
    assert paths == {"/healthz", "/readyz", "/auth/register", "/auth/login", "/auth/me"}


async def test_no_session_or_refresh_endpoint_exists(client: AsyncClient) -> None:
    """Stage 1 issues access tokens only (ADR-0003)."""
    for path in ("/auth/refresh", "/auth/logout", "/auth/sessions"):
        response = await client.get(path)
        assert response.status_code == 404, path
