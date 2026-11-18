"""Health endpoints for the Auth Service."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import engine

logger = logging.getLogger("auth.health")

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {"status": "ok", "service": settings.app_name, "env": settings.app_env}


@router.get("/readyz", summary="Readiness probe")
async def readyz() -> JSONResponse:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # any database failure means "not ready"
        logger.warning("readiness: database unreachable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "checks": {"database": "unavailable"}},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "checks": {"database": "ok"}},
    )
