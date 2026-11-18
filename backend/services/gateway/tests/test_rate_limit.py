"""Rate limiting: quota enforcement, scoping, and fail-open behaviour."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import RateLimit
from app.main import app
from app.middleware.rate_limit import RateLimiter
from tests.fakes import BrokenRedis

WALLET_URL = "http://wallet:8000/accounts"
AUTH_URL = "http://auth:8000/auth/login"


@pytest.fixture
def tiny_limits() -> None:
    app.state.rate_limiter = RateLimiter(
        app.state.redis,
        app.state.settings,
        auth_limit=RateLimit(limit=2, window_seconds=60),
        default_limit=RateLimit(limit=2, window_seconds=60),
    )


@respx.mock
async def test_third_request_is_rate_limited(client, auth_headers, tiny_limits) -> None:
    respx.get(WALLET_URL).mock(return_value=httpx.Response(200, json={}))

    for _ in range(2):
        allowed = await client.get("/api/v1/wallet/accounts", headers=auth_headers)
        assert allowed.status_code == 200

    response = await client.get("/api/v1/wallet/accounts", headers=auth_headers)

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "RATE_LIMITED"
    assert response.headers["retry-after"].isdigit()
    assert response.headers["x-ratelimit-limit"] == "2"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert int(response.headers["x-ratelimit-reset"]) > 0


@respx.mock
async def test_auth_and_default_quotas_are_independent(client, auth_headers, tiny_limits) -> None:
    respx.get(WALLET_URL).mock(return_value=httpx.Response(200, json={}))
    respx.post(AUTH_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))

    # Exhaust the auth quota (public route, no token needed).
    for _ in range(2):
        assert (await client.post("/api/v1/auth/login", json={})).status_code == 200
    assert (await client.post("/api/v1/auth/login", json={})).status_code == 429

    # The default scope is untouched.
    assert (await client.get("/api/v1/wallet/accounts", headers=auth_headers)).status_code == 200


@respx.mock
async def test_redis_outage_fails_open(client, auth_headers) -> None:
    app.state.rate_limiter = RateLimiter(
        BrokenRedis(),  # type: ignore[arg-type]
        app.state.settings,
        auth_limit=RateLimit(limit=1, window_seconds=60),
        default_limit=RateLimit(limit=1, window_seconds=60),
    )
    respx.get(WALLET_URL).mock(return_value=httpx.Response(200, json={}))

    for _ in range(5):
        response = await client.get("/api/v1/wallet/accounts", headers=auth_headers)
        assert response.status_code == 200


@respx.mock
async def test_disabled_limiter_allows_everything(client, auth_headers) -> None:
    settings = app.state.settings.model_copy(update={"rate_limit_enabled": False})
    app.state.rate_limiter = RateLimiter(
        app.state.redis,
        settings,
        default_limit=RateLimit(limit=1, window_seconds=60),
        auth_limit=RateLimit(limit=1, window_seconds=60),
    )
    respx.get(WALLET_URL).mock(return_value=httpx.Response(200, json={}))

    for _ in range(3):
        response = await client.get("/api/v1/wallet/accounts", headers=auth_headers)
        assert response.status_code == 200


async def test_rate_limit_applies_before_jwt_verification(client) -> None:
    """Cheap rejection: an unauthenticated flood must not reach JWT parsing."""
    app.state.rate_limiter = RateLimiter(
        app.state.redis,
        app.state.settings,
        auth_limit=RateLimit(limit=1, window_seconds=60),
        default_limit=RateLimit(limit=1, window_seconds=60),
    )
    first = await client.get("/api/v1/wallet/accounts")
    second = await client.get("/api/v1/wallet/accounts")
    assert first.status_code == 401
    assert second.status_code == 429
