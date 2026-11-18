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
