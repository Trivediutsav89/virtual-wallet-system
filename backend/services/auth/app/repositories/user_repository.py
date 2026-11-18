"""Data access. All SQL for the Auth Service lives here."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        """Case-insensitive lookup that can use ``ux_users_email_lower``."""
        result = await self._session.execute(
            select(User).where(func.lower(User.email) == email.strip().lower())
        )
        return result.scalar_one_or_none()

    async def create(self, *, email: str, password_hash: str) -> User:
        """Insert without a pre-check: the unique index is the source of truth.

        ``flush`` (not ``commit``) surfaces the ``IntegrityError`` to the caller
        so it can translate it into a domain error and control the transaction.
        """
        user = User(email=email, password_hash=password_hash)
        self._session.add(user)
        await self._session.flush()
        return user

    def record_login(self, user: User, when: datetime) -> None:
        user.last_login_at = when
