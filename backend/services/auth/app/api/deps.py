"""Shared FastAPI dependencies."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header

from app.errors import auth_required


async def current_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> uuid.UUID:
    """Read the identity injected by the gateway.

    The Auth Service does **not** parse JWTs on normal requests: the gateway has
    already verified the signature and expiry, and downstream trust is a
    documented Stage 1 boundary (ADR-0004). Direct access to this endpoint
    therefore requires the header, not a bearer token.
    """
    if not x_user_id:
        raise auth_required("Missing identity header.")
    try:
        return uuid.UUID(x_user_id)
    except (TypeError, ValueError) as exc:
        raise auth_required("Malformed identity header.") from exc
