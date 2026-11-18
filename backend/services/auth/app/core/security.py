"""Password hashing and access-token issuing.

Only the Auth Service signs tokens; only the Gateway verifies them.

bcrypt is used directly rather than through ``passlib``: passlib is
unmaintained and its bcrypt backend breaks against bcrypt >= 4.1.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

import anyio.to_thread
import bcrypt
import jwt

from app.config import Settings

__all__ = [
    "IssuedToken",
    "compute_dummy_hash",
    "create_access_token",
    "hash_password",
    "hash_password_async",
    "verify_password",
    "verify_password_async",
]


def hash_password(password: str, cost: int) -> str:
    """Hash a password with bcrypt (``cost`` rounds).

    Callers must enforce the 72-byte limit first: bcrypt would raise, or worse,
    silently truncate on older versions.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=cost)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish comparison. Returns False on malformed hashes."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (TypeError, ValueError):
        return False


async def hash_password_async(password: str, cost: int) -> str:
    return await anyio.to_thread.run_sync(hash_password, password, cost)


async def verify_password_async(password: str, password_hash: str) -> bool:
    return await anyio.to_thread.run_sync(verify_password, password, password_hash)


def compute_dummy_hash(cost: int) -> str:
    """A hash of an unguessable value, used to burn the same CPU time when the
    supplied email does not exist. Prevents timing-based account enumeration."""
    return hash_password(secrets.token_urlsafe(32), cost)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    expires_at: int
    expires_in: int


def create_access_token(
    *,
    user_id: uuid.UUID,
    token_version: int,
    settings: Settings,
    now: datetime | None = None,
) -> IssuedToken:
    """Issue an access token with the approved claim set.

    ``sub, iss, iat, exp, jti, typ, ver`` — no email, name, or role: a JWT is
    signed, not encrypted, and the browser can read the payload.
    """
    issued_at = int(now.timestamp()) if now is not None else int(time.time())
    expires_in = settings.access_token_ttl_seconds
    expires_at = issued_at + expires_in

    payload = {
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "iat": issued_at,
        "exp": expires_at,
        "jti": str(uuid.uuid4()),
        "typ": "access",
        "ver": token_version,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return IssuedToken(token=token, expires_at=expires_at, expires_in=expires_in)
