"""Uniform error envelope for the Auth Service.

Wire format::

    {"detail": {"code": "INVALID_CREDENTIALS", "message": "..."}}

Codes are stable so clients never have to match on prose. Auth-specific
responses deliberately avoid leaking whether an email exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AppError(Exception):
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


def email_already_registered() -> AppError:
    return AppError(
        409,
        "EMAIL_ALREADY_REGISTERED",
        "An account with this email already exists.",
    )


def invalid_credentials() -> AppError:
    """Same response for unknown email and wrong password (no enumeration)."""
    return AppError(
        401,
        "INVALID_CREDENTIALS",
        "Invalid email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def account_inactive() -> AppError:
    return AppError(403, "ACCOUNT_INACTIVE", "This account has been deactivated.")


def auth_required(message: str = "Authentication required.") -> AppError:
    return AppError(401, "AUTH_REQUIRED", message, headers={"WWW-Authenticate": "Bearer"})


def identity_not_found() -> AppError:
    """The injected identity no longer resolves to a user (deleted account)."""
    return AppError(
        401,
        "AUTH_REQUIRED",
        "This identity is no longer valid.",
        headers={"WWW-Authenticate": "Bearer"},
    )
