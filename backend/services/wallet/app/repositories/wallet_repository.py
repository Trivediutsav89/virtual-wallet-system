"""All SQL for the Wallet Service lives here.

Money movement is a single conditional ``UPDATE`` per operation. PostgreSQL
locks the row for the duration of the statement, so concurrent withdrawals
cannot both observe the same starting balance.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.wallet import Wallet


class WalletRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user_id(self, user_id: uuid.UUID) -> Wallet | None:
        result = await self._session.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create(self, user_id: uuid.UUID) -> Wallet | None:
        statement = (
            pg_insert(Wallet)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=["user_id"])
            .returning(Wallet)
        )
        result = await self._session.execute(statement)
        created = result.scalar_one_or_none()
        if created is not None:
            return created
        return await self.get_by_user_id(user_id)

    async def deposit(self, user_id: uuid.UUID, amount: Decimal) -> Wallet | None:
        statement = (
            update(Wallet)
            .where(Wallet.user_id == user_id)
            .values(balance=Wallet.balance + amount, updated_at=func.now())
            .returning(Wallet)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def withdraw(self, user_id: uuid.UUID, amount: Decimal) -> Wallet | None:
        """Return the updated wallet only when the balance covers ``amount``.

        The balance predicate is part of the UPDATE, not a prior SELECT, so the
        database arbitrates concurrent withdrawals. Zero rows means either the
        wallet is missing or the funds are insufficient; the service has just
        lazily created the wallet, so it is the latter.
        """
        statement = (
            update(Wallet)
            .where(Wallet.user_id == user_id, Wallet.balance >= amount)
            .values(balance=Wallet.balance - amount, updated_at=func.now())
            .returning(Wallet)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()


__all__ = ["WalletRepository"]
