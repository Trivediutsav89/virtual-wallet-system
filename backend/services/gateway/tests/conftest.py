"""Shared fixtures for gateway tests.

The suite runs without Docker: Redis is replaced with an in-memory fake and
upstream services are mocked with ``respx``.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator, Callable

import pytest

# Environment must be set before the application module is imported, because
# settings (and therefore the app) are built at import time.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("JWT_SECRET", "test-secret-0123456789abcdef0123456789abcdef")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("JWT_ISSUER", "virtual-wallet-auth")
os.environ.setdefault("AUTH_SERVICE_URL", "http://auth:8000")
os.environ.setdefault("PROFILE_SERVICE_URL", "http://profile:8000")
os.environ.setdefault("WALLET_SERVICE_URL", "http://wallet:8000")
os.environ.setdefault("REDIS_URL", "redis://redis:6379/0")
os.environ.setdefault("RATE_LIMIT_ENABLED", "true")
os.environ.setdefault("RATE_LIMIT_DEFAULT", "1000/min")
os.environ.setdefault("RATE_LIMIT_AUTH", "1000/min")

import jwt
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.middleware.rate_limit import RateLimiter


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the ASGI app with a fake Redis."""
    async with app.router.lifespan_context(app):
        original_redis = app.state.redis
        fake_redis = FakeRedis(decode_responses=True)
        app.state.redis = fake_redis
        app.state.rate_limiter = RateLimiter(fake_redis, app.state.settings)

        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://gateway") as http_client:
                yield http_client
        finally:
            app.state.redis = original_redis
            await fake_redis.aclose()


@pytest.fixture
def make_token() -> Callable[..., str]:
    """Mint a token with the app's real secret, overriding any claim."""

    settings = app.state.settings

    def _make(key: str | None = None, **overrides: object) -> str:
        now = int(time.time())
        payload: dict[str, object] = {
            "sub": str(uuid.uuid4()),
            "iss": settings.jwt_issuer,
            "iat": now,
            "exp": now + 1800,
            "jti": uuid.uuid4().hex,
            "typ": "access",
            "ver": 0,
        }
        payload.update(overrides)
        payload = {claim: value for claim, value in payload.items() if value is not None}
        return jwt.encode(payload, key or settings.jwt_secret, algorithm="HS256")

    return _make


@pytest.fixture
def auth_headers(make_token: Callable[..., str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token()}"}
