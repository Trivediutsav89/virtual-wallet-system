"""Trusted identity dependency for the Wallet Service."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header

from app.errors import auth_required


async def current_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> uuid.UUID:
    """Return the identity injected by the gateway after JWT verification."""
    if not x_user_id:
        raise auth_required("Missing identity header.")
    try:
        return uuid.UUID(x_user_id)
    except (TypeError, ValueError) as exc:
        raise auth_required("Malformed identity header.") from exc
