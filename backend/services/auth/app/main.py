"""Auth Service application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.config import Settings, get_settings
from app.db import engine
from app.errors import AppError
from app.logging import configure_logging
from app.middleware.request_context import RequestContextMiddleware

logger = logging.getLogger("auth")

_HTTP_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "AUTH_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("service started", extra={"event": "startup"})
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("service stopped", extra={"event": "shutdown"})


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_body(),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Keep FastAPI's 422 shape but never echo the submitted value.

        FastAPI normally includes ``input`` in every validation error. That
        would reflect a plaintext password (and, for a non-object body, the
        entire payload) into the response body and any client-side log, so the
        field is dropped unconditionally.
        """
        errors = exc.errors()
        for error in errors:
            error.pop("input", None)
            context = error.get("ctx")
            if isinstance(context, dict):
                error["ctx"] = {key: str(value) for key, value in context.items()}
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(errors)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {
                    "code": _HTTP_STATUS_CODES.get(exc.status_code, "HTTP_ERROR"),
                    "message": str(exc.detail),
                }
            },
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error on %s %s",
            request.method,
            request.url.path,
            extra={"event": "unhandled_error"},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": {"code": "INTERNAL_ERROR", "message": "Internal server error."}},
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.app_name)

    app = FastAPI(
        title="Virtual Wallet Auth Service",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "prod" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env != "prod" else None,
    )
    app.state.settings = settings

    # No CORS here: the gateway is the only public origin (ADR-0004).
    app.add_middleware(RequestContextMiddleware)

    _register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    return app


app = create_app()
