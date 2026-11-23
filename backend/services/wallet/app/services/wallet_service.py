"""Wallet business logic.

All operations are keyed by the trusted gateway-injected user id. Deposit and
withdrawal are simulated local balance changes — no payment gateway, transfers
or ledger exist in Day 2 (ADR-0005).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import insufficient_funds, wallet_unavailable
from app.models.wallet import Wallet
from app.repositories.wallet_repository import WalletRepository

logger = logging.getLogger("wallet.service")


class WalletService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._wallets = WalletRepository(session)

    async def get_or_create(self, user_id: uuid.UUID) -> Wallet:
        wallet = await self._wallets.get_or_create(user_id)
        if wallet is None:
            raise wallet_unavailable()
        await self._session.commit()
        await self._session.refresh(wallet)
        return wallet

    async def deposit(self, user_id: uuid.UUID, amount: Decimal) -> Wallet:
        await self.get_or_create(user_id)
        wallet = await self._wallets.deposit(user_id, amount)
        if wallet is None:
            raise wallet_unavailable()
        await self._session.commit()
        await self._session.refresh(wallet)
        logger.info("deposit applied", extra={"event": "deposit"})
        return wallet

    async def withdraw(self, user_id: uuid.UUID, amount: Decimal) -> Wallet:
        await self.get_or_create(user_id)
        wallet = await self._wallets.withdraw(user_id, amount)
        if wallet is None:
            raise insufficient_funds()
        await self._session.commit()
        await self._session.refresh(wallet)
        logger.info("withdrawal applied", extra={"event": "withdrawal"})
        return wallet


__all__ = ["WalletService"]
