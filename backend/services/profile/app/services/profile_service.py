"""Profile business logic.

Every operation is keyed by the trusted gateway-injected user id. Profiles are
created lazily, so registration never fans out to this service (ADR-0001).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import profile_unavailable
from app.models.profile import Profile
from app.repositories.profile_repository import ProfileRepository
from app.schemas.profile import ProfilePatchRequest

logger = logging.getLogger("profile.service")


class ProfileService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._profiles = ProfileRepository(session)

    async def get_or_create(self, user_id: uuid.UUID) -> Profile:
        profile = await self._profiles.get_or_create(user_id)
        if profile is None:
            raise profile_unavailable()
        await self._session.commit()
        await self._session.refresh(profile)
        return profile

    async def update(self, user_id: uuid.UUID, patch: ProfilePatchRequest) -> Profile:
        """Apply a partial patch, lazily creating the profile if needed."""
        profile = await self.get_or_create(user_id)
        for field, value in patch.model_dump(exclude_unset=True).items():
            # Only schema-declared fields are present here: extra="forbid"
            # rejects user_id, id and timestamps before they reach this point.
            setattr(profile, field, value)
        await self._session.commit()
        await self._session.refresh(profile)
        logger.info("profile updated", extra={"event": "profile_updated"})
        return profile


__all__ = ["ProfileService"]
