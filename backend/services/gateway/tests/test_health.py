"""Health and readiness endpoints."""

from __future__ import annotations

import httpx
import respx

from app.main import app
from app.middleware.rate_limit import RateLimiter


async def test_healthz_does_not_touch_dependencies(client) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "gateway"


@respx.mock
async def test_readyz_reports_all_dependencies(client) -> None:
    for name in ("auth", "profile", "wallet"):
        respx.get(f"http://{name}:8000/healthz").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

    response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {
        "redis": "ok",
        "auth": "ok",
        "profile": "ok",
        "wallet": "ok",
    }


@respx.mock
async def test_readyz_reports_degraded_upstream_but_stays_ready(client) -> None:
    respx.get("http://auth:8000/healthz").mock(side_effect=httpx.ConnectError("down"))
    respx.get("http://profile:8000/healthz").mock(return_value=httpx.Response(200))
    respx.get("http://wallet:8000/healthz").mock(return_value=httpx.Response(200))

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["auth"] == "unavailable"


async def test_readyz_is_unavailable_when_redis_is_down(client) -> None:
    from tests.fakes import BrokenRedis

    original = app.state.redis
    app.state.redis = BrokenRedis()
    try:
        response = await client.get("/readyz")
    finally:
        app.state.redis = original
        app.state.rate_limiter = RateLimiter(original, app.state.settings)

    assert response.status_code == 503
    assert response.json()["checks"]["redis"] == "unavailable"
