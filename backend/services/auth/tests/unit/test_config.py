"""Configuration guarantees that the rest of the system relies on."""

from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "jwt_secret": "x" * 32,
        "auth_database_url": "postgresql+asyncpg://auth_svc:pw@localhost:5432/auth_db",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_access_token_ttl_defaults_to_thirty_minutes() -> None:
    # Read the declared default, not an instance: the test environment sets
    # BCRYPT_COST/ACCESS_TOKEN_TTL_MINUTES to keep the suite fast.
    assert Settings.model_fields["access_token_ttl_minutes"].default == 30


def test_effective_access_token_ttl_is_thirty_minutes() -> None:
    from app.config import get_settings

    assert get_settings().access_token_ttl_seconds == 1800


def test_bcrypt_cost_defaults_to_twelve() -> None:
    assert Settings.model_fields["bcrypt_cost"].default == 12


def test_jwt_algorithm_is_symmetric_only() -> None:
    with pytest.raises(ValueError):
        _settings(jwt_algorithm="RS256")


def test_short_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValueError):
        _settings(jwt_secret="too-short")


def test_database_url_must_use_asyncpg() -> None:
    with pytest.raises(ValueError):
        _settings(auth_database_url="postgresql://auth_svc:pw@localhost:5432/auth_db")


@pytest.mark.parametrize("ttl", [0, -5, 1441])
def test_ttl_bounds(ttl: int) -> None:
    with pytest.raises(ValueError):
        _settings(access_token_ttl_minutes=ttl)


@pytest.mark.parametrize("cost", [3, 17])
def test_bcrypt_cost_bounds(cost: int) -> None:
    with pytest.raises(ValueError):
        _settings(bcrypt_cost=cost)
