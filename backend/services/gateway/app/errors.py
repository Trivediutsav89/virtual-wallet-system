"""Uniform error envelope for the gateway.

Wire format::

    {"detail": {"code": "INVALID_TOKEN", "message": "..."}}

``code`` is stable and machine-readable; ``message`` is safe to display.
Internal details are logged, never returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class GatewayError(Exception):
    """An error that maps directly onto an HTTP response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})

    def to_body(self) -> dict[str, Any]:
        return {"detail": {"code": self.code, "message": self.message}}


def auth_required(message: str = "Authentication required.") -> GatewayError:
    return GatewayError(
        401,
        "AUTH_REQUIRED",
        message,
        headers={"WWW-Authenticate": "Bearer"},
    )


def invalid_token(message: str = "Invalid or expired access token.") -> GatewayError:
    return GatewayError(
        401,
        "INVALID_TOKEN",
        message,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def rate_limited(
    *,
    limit: int,
    remaining: int,
    reset_at: int,
    retry_after: int,
) -> GatewayError:
    return GatewayError(
        429,
        "RATE_LIMITED",
        "Too many requests. Slow down and retry later.",
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_at),
        },
    )


def not_found(message: str = "Resource not found.") -> GatewayError:
    return GatewayError(404, "NOT_FOUND", message)


def bad_gateway(message: str = "Upstream service is unavailable.") -> GatewayError:
    return GatewayError(502, "UPSTREAM_UNAVAILABLE", message)


def gateway_timeout(message: str = "Upstream service timed out.") -> GatewayError:
    return GatewayError(504, "UPSTREAM_TIMEOUT", message)
