"""Auth business logic.

Registration deliberately performs **no fan-out**: it never calls the Profile or
Wallet services. Those create their own rows lazily on first authenticated
request, which removes the need for a distributed transaction (ADR-0001/0004).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.security import (
    IssuedToken,
    compute_dummy_hash,
    create_access_token,
    hash_password_async,
    verify_password_async,
)
from app.errors import (
    account_inactive,
    email_already_registered,
    identity_not_found,
    invalid_credentials,
)
from app.models.user import User
from app.repositories.user_repository import UserRepository

logger = logging.getLogger("auth.service")


@lru_cache(maxsize=4)
def _dummy_hash(cost: int) -> str:
    return compute_dummy_hash(cost)


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._settings = settings

    async def register(self, *, email: str, password: str) -> User:
        """Create a user. Duplicate email => 409 (via the DB unique index)."""
        password_hash = await hash_password_async(password, self._settings.bcrypt_cost)

        try:
            user = await self._users.create(email=email, password_hash=password_hash)
        except IntegrityError as exc:
            await self._session.rollback()
            logger.info(
                "registration rejected: email already registered",
                extra={"event": "register_conflict"},
            )
            raise email_already_registered() from exc

        await self._session.commit()
        await self._session.refresh(user)
        logger.info(
            "user registered",
            extra={"event": "user_registered"},
        )
        return user

    async def login(self, *, email: str, password: str) -> tuple[User, IssuedToken]:
        """Verify credentials and issue an access token.

        Unknown email and wrong password produce the same error and the same
        amount of bcrypt work, so neither timing nor response shape reveals
        whether an account exists.
        """
        user = await self._users.get_by_email(email)

        if user is None:
            await verify_password_async(
                password, _dummy_hash(self._settings.bcrypt_cost)
            )
            raise invalid_credentials()

        if not await verify_password_async(password, user.password_hash):
            raise invalid_credentials()

        if not user.is_active:
            raise account_inactive()

        self._users.record_login(user, datetime.now(UTC))
        await self._session.commit()
        await self._session.refresh(user)

        token = create_access_token(
            user_id=user.id,
            token_version=user.token_version,
            settings=self._settings,
        )
        logger.info("login succeeded", extra={"event": "login_success"})
        return user, token

    async def get_user(self, user_id: uuid.UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            # Do not reveal whether the id ever existed.
            raise identity_not_found()
        if not user.is_active:
            raise account_inactive()
        return user
