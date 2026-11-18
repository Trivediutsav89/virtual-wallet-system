"""Pydantic v2 request/response models.

Passwords are wrapped in ``SecretStr`` so they cannot leak into logs, reprs, or
FastAPI's validation errors (which echo ``input`` for ordinary fields).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, SecretStr, field_validator

from app.config import BCRYPT_MAX_PASSWORD_BYTES

PASSWORD_MIN_LENGTH = 8
EMAIL_MAX_LENGTH = 254  # RFC 5321


def _check_password_bytes(raw: str) -> None:
    if len(raw.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes "
            "(this is a bcrypt limit, not a product rule)."
        )


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: SecretStr

    @field_validator("email", mode="before")
    @classmethod
    def _strip_email(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def _limit_email_length(cls, value: EmailStr) -> EmailStr:
        if len(str(value)) > EMAIL_MAX_LENGTH:
            raise ValueError(f"Email must be at most {EMAIL_MAX_LENGTH} characters")
        return value

    @field_validator("password")
    @classmethod
    def _password_policy(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        _check_password_bytes(raw)
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: SecretStr

    @field_validator("email", mode="before")
    @classmethod
    def _strip_email(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("password")
    @classmethod
    def _non_empty_and_within_bcrypt_limit(cls, value: SecretStr) -> SecretStr:
        # No complexity policy on login: an existing credential must simply be
        # checked, not re-validated. We only reject what bcrypt cannot process.
        raw = value.get_secret_value()
        if not raw:
            raise ValueError("Password must not be empty")
        _check_password_bytes(raw)
        return value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    is_active: bool
    is_verified: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - a scheme name, not a secret
    expires_in: int
