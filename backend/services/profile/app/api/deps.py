"""Shared FastAPI dependencies for the Profile Service.

The gateway is the only source of identity: it verifies the JWT, strips all
client-supplied identity headers and injects ``X-User-Id``. Profile never
parses tokens and never accepts a user id from a request body or query string.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header

from app.errors import auth_required


async def current_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> uuid.UUID:
    """Return the trusted identity injected by the gateway."""
    if not x_user_id:
        raise auth_required("Missing identity header.")
    try:
        return uuid.UUID(x_user_id)
    except (TypeError, ValueError) as exc:
        raise auth_required("Malformed identity header.") from exc
