"""All SQL for the Profile Service lives here."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile


class ProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user_id(self, user_id: uuid.UUID) -> Profile | None:
        result = await self._session.execute(
            select(Profile).where(Profile.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create(self, user_id: uuid.UUID) -> Profile | None:
        """Atomically insert a profile or return the existing one.

        ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` makes concurrent first
        accesses idempotent: the unique index arbitrates the race in Postgres,
        so there is no pre-check to lose and no unhandled ``IntegrityError``.
        """
        statement = (
            pg_insert(Profile)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=["user_id"])
            .returning(Profile)
        )
        result = await self._session.execute(statement)
        created = result.scalar_one_or_none()
        if created is not None:
            return created
        return await self.get_by_user_id(user_id)


__all__ = ["ProfileRepository"]
