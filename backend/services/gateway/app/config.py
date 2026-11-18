"""Configuration for the API Gateway.

All values come from environment variables (or a local ``.env`` in development)
and are validated at startup so the process fails fast on misconfiguration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_RATE_LIMIT_PATTERN = re.compile(
    r"^\s*(?P<count>\d+)\s*/\s*(?P<unit>s|sec|second|m|min|minute|h|hour)\s*$"
)
_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hour": 3600,
}


@dataclass(frozen=True, slots=True)
class RateLimit:
    """A fixed-window quota, e.g. ``120/min``."""

    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("rate limit count must be >= 1")
        if self.window_seconds < 1:
            raise ValueError("rate limit window must be >= 1 second")


def parse_rate_limit(value: str) -> RateLimit:
    """Parse ``"<count>/<unit>"`` into a :class:`RateLimit`.

    Accepted units: ``s``, ``sec``, ``second``, ``m``, ``min``, ``minute``,
    ``h``, ``hour``.
    """
    match = _RATE_LIMIT_PATTERN.match(value.strip().lower())
    if match is None:
        raise ValueError(f"invalid rate limit {value!r}; expected e.g. '120/min'")
    return RateLimit(
        limit=int(match.group("count")),
        window_seconds=_UNIT_SECONDS[match.group("unit")],
    )


class Settings(BaseSettings):
    """Runtime configuration. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "gateway"
    app_env: Literal["local", "test", "ci", "prod"] = "local"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    # --- JWT (verification only; the gateway never signs tokens) -----------
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "virtual-wallet-auth"
    jwt_leeway_seconds: int = Field(default=30, ge=0, le=300)

    # --- Upstreams --------------------------------------------------------
    auth_service_url: str
    profile_service_url: str
    wallet_service_url: str
    http_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    # --- Rate limiting ----------------------------------------------------
    redis_url: str = "redis://redis:6379/0"
    rate_limit_enabled: bool = True
    rate_limit_default: str = "120/min"
    rate_limit_auth: str = "10/min"
    trust_forwarded_for: bool = False

    # --- CORS -------------------------------------------------------------
    cors_origins: str = "http://localhost:3000"

    @field_validator("auth_service_url", "profile_service_url", "wallet_service_url")
    @classmethod
    def _normalise_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("upstream URL must start with http:// or https://")
        return value

    @field_validator("jwt_algorithm")
    @classmethod
    def _only_hmac(cls, value: str) -> str:
        if value not in {"HS256", "HS384", "HS512"}:
            raise ValueError("gateway supports symmetric algorithms only (HS256/384/512)")
        return value

    @field_validator("rate_limit_default", "rate_limit_auth")
    @classmethod
    def _valid_rate_limit(cls, value: str) -> str:
        parse_rate_limit(value)
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def upstream_urls(self) -> dict[str, str]:
        return {
            "auth": self.auth_service_url,
            "profile": self.profile_service_url,
            "wallet": self.wallet_service_url,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Values come from the environment; mypy cannot see pydantic-settings'
    # runtime population of required fields.
    return Settings()  # type: ignore[call-arg]
