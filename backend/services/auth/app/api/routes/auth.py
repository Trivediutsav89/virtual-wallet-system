"""Auth endpoints: registration, login, current user."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id
from app.config import Settings, get_settings
from app.db import get_session
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.services.auth_service import AuthService

logger = logging.getLogger("auth.routes")

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentUserDep = Annotated[uuid.UUID, Depends(current_user_id)]


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    responses={
        409: {"description": "Email already registered"},
        422: {"description": "Validation error"},
    },
)
async def register(
    payload: RegisterRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> UserOut:
    service = AuthService(session, settings)
    user = await service.register(
        email=str(payload.email),
        password=payload.password.get_secret_value(),
    )
    return UserOut.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for an access token",
    responses={
        401: {"description": "Invalid credentials"},
        403: {"description": "Account inactive"},
    },
)
async def login(
    payload: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> TokenResponse:
    service = AuthService(session, settings)
    _, token = await service.login(
        email=str(payload.email),
        password=payload.password.get_secret_value(),
    )
    return TokenResponse(access_token=token.token, expires_in=token.expires_in)


@router.get(
    "/me",
    response_model=UserOut,
    summary="Return the authenticated user",
    responses={401: {"description": "Missing or invalid identity header"}},
)
async def me(
    user_id: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
) -> UserOut:
    service = AuthService(session, settings)
    user = await service.get_user(user_id)
    return UserOut.model_validate(user)
