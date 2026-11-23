"""Uniform error envelope shared by every service.

Wire format::

    {"detail": {"code": "SOME_CODE", "message": "..."}}
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


def auth_required(message: str = "Authentication required.") -> AppError:
    return AppError(
        401,
        "AUTH_REQUIRED",
        message,
        headers={"WWW-Authenticate": "Bearer"},
    )


def insufficient_funds() -> AppError:
    """The conditional withdrawal update matched no row with enough balance."""
    return AppError(
        409,
        "INSUFFICIENT_FUNDS",
        "Insufficient balance for this withdrawal.",
    )


def wallet_unavailable() -> AppError:
    """The wallet disappeared between lazy creation and the money operation."""
    return AppError(500, "INTERNAL_ERROR", "Wallet could not be resolved.")
