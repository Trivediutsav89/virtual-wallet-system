"""Unit tests for configuration parsing and routing helpers."""

from __future__ import annotations

import uuid

import pytest
from starlette.datastructures import Headers

from app.config import RateLimit, Settings, parse_rate_limit
from app.errors import GatewayError, invalid_token
from app.routing.proxy import (
    Route,
    build_upstream_headers,
    downstream_path,
    match_route,
)
from app.security.jwt import Principal


@pytest.mark.parametrize(
    ("raw", "limit", "window"),
    [
        ("120/min", 120, 60),
        ("10/m", 10, 60),
        ("5/minute", 5, 60),
        ("30/s", 30, 1),
        ("7/sec", 7, 1),
        ("100/h", 100, 3600),
        (" 3 / HOUR ", 3, 3600),
    ],
)
def test_parse_rate_limit_accepts_known_units(raw: str, limit: int, window: int) -> None:
    parsed = parse_rate_limit(raw)
    assert parsed == RateLimit(limit=limit, window_seconds=window)


@pytest.mark.parametrize("raw", ["", "10", "10/", "/min", "ten/min", "10/day", "-1/min", "0/min"])
def test_parse_rate_limit_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_rate_limit(raw)


def test_rate_limit_requires_positive_values() -> None:
    with pytest.raises(ValueError):
        RateLimit(limit=0, window_seconds=60)
    with pytest.raises(ValueError):
        RateLimit(limit=10, window_seconds=0)


def test_settings_require_a_long_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(ValueError):
        # jwt_secret comes from the environment set above.
        Settings(  # type: ignore[call-arg]
            auth_service_url="http://auth:8000",
            profile_service_url="http://profile:8000",
            wallet_service_url="http://wallet:8000",
        )


def test_settings_reject_non_http_upstream() -> None:
    with pytest.raises(ValueError):
        Settings(
            jwt_secret="x" * 32,
            auth_service_url="auth:8000",
            profile_service_url="http://profile:8000",
            wallet_service_url="http://wallet:8000",
        )


def test_settings_reject_asymmetric_algorithm() -> None:
    with pytest.raises(ValueError):
        Settings(
            jwt_secret="x" * 32,
            jwt_algorithm="RS256",
            auth_service_url="http://auth:8000",
            profile_service_url="http://profile:8000",
            wallet_service_url="http://wallet:8000",
        )


def test_downstream_path_strips_api_prefix() -> None:
    assert downstream_path("/api/v1", "auth/login") == "/auth/login"
    assert downstream_path("/api/v1", "/auth/login") == "/auth/login"
    assert downstream_path("/api/v1", "") == "/"


def test_route_matching_respects_segment_boundaries() -> None:
    routes = (
        Route(name="auth", prefix="/auth", upstream="http://auth:8000"),
        Route(name="wallet", prefix="/wallet", upstream="http://wallet:8000", strip_prefix=True),
        Route(
            name="wallet-admin",
            prefix="/wallet/admin",
            upstream="http://wallet:8000",
            strip_prefix=True,
        ),
    )
    assert match_route(routes, "/auth/login").name == "auth"  # type: ignore[union-attr]
    assert match_route(routes, "/wallet/accounts").name == "wallet"  # type: ignore[union-attr]
    # Longest prefix wins.
    assert match_route(routes, "/wallet/admin/stats").name == "wallet-admin"  # type: ignore[union-attr]
    # "/walletxyz" is not "/wallet".
    assert match_route(routes, "/walletxyz/accounts") is None


def test_upstream_path_handles_prefix_stripping() -> None:
    auth = Route(name="auth", prefix="/auth", upstream="http://auth:8000")
    wallet = Route(
        name="wallet", prefix="/wallet", upstream="http://wallet:8000", strip_prefix=True
    )
    assert auth.upstream_path("/auth/login") == "/auth/login"
    assert wallet.upstream_path("/wallet/accounts") == "/accounts"
    assert wallet.upstream_path("/wallet") == "/"


def test_public_path_matching_ignores_trailing_slash() -> None:
    route = Route(
        name="auth",
        prefix="/auth",
        upstream="http://auth:8000",
        public_paths=frozenset({"/auth/login", "/auth/register"}),
    )
    assert route.is_public("/auth/login")
    assert route.is_public("/auth/login/")
    assert not route.is_public("/auth/me")


def test_upstream_headers_drop_spoofable_identity_and_auth() -> None:
    incoming = Headers(
        raw=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer secret"),
            (b"x-user-id", b"attacker"),
            (b"x-internal-role", b"admin"),
            (b"x-token-version", b"99"),
            (b"x-forwarded-for", b"1.2.3.4"),
            (b"x-request-id", b"client-supplied"),
            (b"cookie", b"session=1"),
        ]
    )
    principal = Principal(
        user_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        token_version=3,
        token_id="jti-1",
        expires_at=0,
    )
    forwarded = dict(build_upstream_headers(incoming, principal=principal, request_id="rid"))
    assert "authorization" not in forwarded
    assert "cookie" not in forwarded
    assert forwarded["x-user-id"] == "11111111-2222-3333-4444-555555555555"
    assert forwarded["x-token-version"] == "3"
    assert forwarded["x-token-jti"] == "jti-1"
    assert forwarded["x-request-id"] == "rid"
    assert "x-internal-role" not in forwarded
    assert "x-forwarded-for" not in forwarded
    assert forwarded["content-type"] == "application/json"


def test_gateway_error_body_shape() -> None:
    error = invalid_token()
    assert isinstance(error, GatewayError)
    assert error.status_code == 401
    assert error.to_body() == {
        "detail": {"code": "INVALID_TOKEN", "message": "Invalid or expired access token."}
    }
    assert error.headers["WWW-Authenticate"].startswith("Bearer")
