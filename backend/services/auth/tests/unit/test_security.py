"""Password hashing and token issuing (pure functions, no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import jwt
import pytest

from app.config import Settings
from app.core.security import (
    create_access_token,
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)

COST = 4  # fast for tests; production default is asserted in test_config


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "jwt_secret": "x" * 32,
        "auth_database_url": "postgresql+asyncpg://auth_svc:pw@localhost:5432/auth_db",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# bcrypt
# ---------------------------------------------------------------------------


def test_hash_is_bcrypt_and_verifies() -> None:
    password = "correct-horse-battery"
    hashed = hash_password(password, COST)
    assert hashed.startswith("$2b$")
    assert hashed != password
    assert password not in hashed
    assert verify_password(password, hashed)


def test_hash_is_salted() -> None:
    password = "correct-horse-battery"
    assert hash_password(password, COST) != hash_password(password, COST)


def test_wrong_password_fails() -> None:
    hashed = hash_password("correct-horse-battery", COST)
    assert not verify_password("correct-horse-batterY", hashed)
    assert not verify_password("", hashed)


def test_padding_is_significant() -> None:
    hashed = hash_password("password ", COST)
    assert verify_password("password ", hashed)
    assert not verify_password("password", hashed)


def test_malformed_hash_returns_false_instead_of_raising() -> None:
    assert not verify_password("whatever", "not-a-bcrypt-hash")


def test_password_of_exactly_72_bytes_is_supported() -> None:
    password = "a" * 72
    hashed = hash_password(password, COST)
    assert verify_password(password, hashed)


async def test_async_hash_and_verify_match_sync() -> None:
    hashed = await hash_password_async("async-password", COST)
    assert await verify_password_async("async-password", hashed)
    assert not await verify_password_async("wrong", hashed)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def test_token_claims_match_the_approved_schema() -> None:
    user_id = uuid.uuid4()
    issued_at = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    issued = create_access_token(
        user_id=user_id,
        token_version=3,
        settings=_settings(),
        now=issued_at,
    )

    payload = jwt.decode(
        issued.token,
        "x" * 32,
        algorithms=["HS256"],
        issuer="virtual-wallet-auth",
        # Structure is asserted here; time claims are covered separately, so
        # the fixed timestamp is not compared against the wall clock.
        options={
            "require": ["exp", "iat", "sub", "iss", "typ", "jti"],
            "verify_exp": False,
            "verify_iat": False,
        },
    )

    assert set(payload) == {"sub", "iss", "iat", "exp", "jti", "typ", "ver"}
    assert payload["sub"] == str(user_id)
    assert payload["iss"] == "virtual-wallet-auth"
    assert payload["typ"] == "access"
    assert payload["ver"] == 3
    assert payload["iat"] == int(issued_at.timestamp())
    assert payload["exp"] - payload["iat"] == 1800
    assert issued.expires_in == 1800
    assert issued.expires_at == payload["exp"]


def test_token_contains_no_pii() -> None:
    issued = create_access_token(user_id=uuid.uuid4(), token_version=0, settings=_settings())
    payload = jwt.decode(
        issued.token,
        "x" * 32,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert "email" not in payload
    assert "name" not in payload
    assert "role" not in payload


def test_jti_is_unique_per_token() -> None:
    user_id = uuid.uuid4()
    first = create_access_token(user_id=user_id, token_version=0, settings=_settings())
    second = create_access_token(user_id=user_id, token_version=0, settings=_settings())
    first_payload = jwt.decode(first.token, "x" * 32, algorithms=["HS256"])
    second_payload = jwt.decode(second.token, "x" * 32, algorithms=["HS256"])
    assert first_payload["jti"] != second_payload["jti"]


@pytest.mark.parametrize("ttl", [1, 30, 60])
def test_token_lifetime_follows_configuration(ttl: int) -> None:
    issued = create_access_token(
        user_id=uuid.uuid4(),
        token_version=0,
        settings=_settings(access_token_ttl_minutes=ttl),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    payload = jwt.decode(
        issued.token,
        "x" * 32,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert payload["exp"] - payload["iat"] == ttl * 60
