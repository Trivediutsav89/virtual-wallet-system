"""Profile endpoints exposed at the service root.

The gateway strips the ``/profile`` prefix, so these are reached externally as
``/api/v1/profile/me``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id
from app.db import get_session
from app.schemas.profile import ProfileOut, ProfilePatchRequest
from app.services.profile_service import ProfileService

router = APIRouter(tags=["profile"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[uuid.UUID, Depends(current_user_id)]


@router.get(
    "/me",
    response_model=ProfileOut,
    summary="Get (or lazily create) the authenticated user's profile",
    responses={401: {"description": "Missing or invalid identity header"}},
)
async def get_me(user_id: CurrentUserDep, session: SessionDep) -> ProfileOut:
    profile = await ProfileService(session).get_or_create(user_id)
    return ProfileOut.model_validate(profile)


@router.patch(
    "/me",
    response_model=ProfileOut,
    status_code=status.HTTP_200_OK,
    summary="Partially update the authenticated user's profile",
    responses={
        401: {"description": "Missing or invalid identity header"},
        422: {"description": "Validation error"},
    },
)
async def patch_me(
    payload: ProfilePatchRequest,
    user_id: CurrentUserDep,
    session: SessionDep,
) -> ProfileOut:
    profile = await ProfileService(session).update(user_id, payload)
    return ProfileOut.model_validate(profile)


__all__ = ["router"]
