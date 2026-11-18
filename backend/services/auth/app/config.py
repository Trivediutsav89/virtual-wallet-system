"""Auth Service configuration.

Every value is validated at startup: a missing secret or a malformed DSN stops
the container immediately instead of failing on the first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# bcrypt only considers the first 72 bytes of a password; silent truncation
# would make two different passwords equivalent. We reject instead.
BCRYPT_MAX_PASSWORD_BYTES = 72


class DatabaseSettings(BaseSettings):
    """The subset Alembic needs.

    Migrations must not require the JWT secret: the one-shot ``auth-migrate``
    container only receives the DSN, and a migration job should have no reason
    to hold a signing key.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    auth_database_url: str

    @field_validator("auth_database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("AUTH_DATABASE_URL must use the postgresql+asyncpg:// driver")
        return value


class Settings(DatabaseSettings):
    app_name: str = "auth-service"
    app_env: Literal["local", "test", "ci", "prod"] = "local"
    log_level: str = "INFO"

    # --- Database pool (auth_db only; this service cannot reach profile/wallet)
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_echo: bool = False

    # --- JWT --------------------------------------------------------------
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "virtual-wallet-auth"
    access_token_ttl_minutes: int = Field(default=30, ge=1, le=1440)

    # --- Password hashing -------------------------------------------------
    bcrypt_cost: int = Field(default=12, ge=4, le=16)

    @property
    def access_token_ttl_seconds(self) -> int:
        return self.access_token_ttl_minutes * 60


def get_database_url() -> str:
    """DSN for Alembic; reads only ``AUTH_DATABASE_URL``."""
    return DatabaseSettings().auth_database_url  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Required fields are populated from the environment at runtime.
    return Settings()  # type: ignore[call-arg]
