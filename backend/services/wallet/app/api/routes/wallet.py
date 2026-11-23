"""Wallet endpoints exposed at the service root.

The gateway strips the ``/wallet`` prefix, so these are reached externally as
``/api/v1/wallet/me``, ``/api/v1/wallet/deposit`` and ``/api/v1/wallet/withdraw``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id
from app.db import get_session
from app.schemas.wallet import AmountRequest, WalletOut
from app.services.wallet_service import WalletService

router = APIRouter(tags=["wallet"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[uuid.UUID, Depends(current_user_id)]


@router.get(
    "/me",
    response_model=WalletOut,
    summary="Get (or lazily create) the authenticated user's wallet",
    responses={401: {"description": "Missing or invalid identity header"}},
)
async def get_me(user_id: CurrentUserDep, session: SessionDep) -> WalletOut:
    wallet = await WalletService(session).get_or_create(user_id)
    return WalletOut.model_validate(wallet)


@router.post(
    "/deposit",
    response_model=WalletOut,
    summary="Simulate a deposit into the authenticated user's wallet",
    responses={
        401: {"description": "Missing or invalid identity header"},
        422: {"description": "Validation error"},
    },
)
async def deposit(
    payload: AmountRequest,
    user_id: CurrentUserDep,
    session: SessionDep,
) -> WalletOut:
    wallet = await WalletService(session).deposit(user_id, payload.amount)
    return WalletOut.model_validate(wallet)


@router.post(
    "/withdraw",
    response_model=WalletOut,
    summary="Withdraw from the authenticated user's wallet",
    responses={
        401: {"description": "Missing or invalid identity header"},
        409: {"description": "Insufficient funds"},
        422: {"description": "Validation error"},
    },
)
async def withdraw(
    payload: AmountRequest,
    user_id: CurrentUserDep,
    session: SessionDep,
) -> WalletOut:
    wallet = await WalletService(session).withdraw(user_id, payload.amount)
    return WalletOut.model_validate(wallet)


__all__ = ["router"]
