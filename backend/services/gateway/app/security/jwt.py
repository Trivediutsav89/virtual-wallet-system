"""Access-token verification (the gateway never issues tokens)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import jwt

from app.config import Settings
from app.errors import auth_required, invalid_token

_REQUIRED_CLAIMS = ("exp", "iat", "sub", "iss", "typ")


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified identity injected into downstream requests."""

    user_id: uuid.UUID
    token_version: int
    token_id: str
    expires_at: int


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.strip():
        raise auth_required("Missing Authorization header.")
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        raise invalid_token("Authorization header must use the Bearer scheme.")
    return credentials.strip()


def decode_access_token(token: str, settings: Settings) -> Principal:
    """Verify signature, expiry, issuer and token type.

    Every failure mode collapses into the same 401 response so callers cannot
    probe *why* a token was rejected.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": list(_REQUIRED_CLAIMS)},
            leeway=timedelta(seconds=settings.jwt_leeway_seconds),
        )
    except jwt.PyJWTError as exc:  # includes ExpiredSignature, InvalidSignature...
        raise invalid_token() from exc

    if payload.get("typ") != "access":
        raise invalid_token()

    subject = payload.get("sub")
    try:
        user_id = uuid.UUID(str(subject))
    except (TypeError, ValueError) as exc:
        raise invalid_token() from exc

    version = payload.get("ver", 0)
    if not isinstance(version, int) or version < 0:
        raise invalid_token()

    token_id = payload.get("jti")
    if not isinstance(token_id, str) or not token_id:
        raise invalid_token()

    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        raise invalid_token()

    return Principal(
        user_id=user_id,
        token_version=version,
        token_id=token_id,
        expires_at=expires_at,
    )


def authenticate(authorization: str | None, settings: Settings) -> Principal:
    """Extract and verify the bearer token from a request header."""
    return decode_access_token(_extract_bearer(authorization), settings)
