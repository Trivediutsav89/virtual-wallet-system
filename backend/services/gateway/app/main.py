"""API Gateway application factory.

Responsibilities (ADR-0004): routing, JWT verification, rate limiting, request
ids, CORS, uniform errors. No database, no business logic, no token signing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.config import Settings, get_settings
from app.errors import GatewayError
from app.logging import configure_logging
from app.middleware.rate_limit import RateLimiter
from app.middleware.request_context import RequestContextMiddleware
from app.routing.health import router as health_router
from app.routing.proxy import build_routes, proxy_request

logger = logging.getLogger("gateway")

_PROXIED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

_HTTP_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "AUTH_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.http_timeout_seconds,
            connect=5.0,
            pool=5.0,
        ),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        follow_redirects=False,
    )
    app.state.redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    app.state.rate_limiter = RateLimiter(app.state.redis, settings)

    logger.info(
        "gateway started",
        extra={"event": "startup", "path": settings.api_prefix},
    )
    try:
        yield
    finally:
        await app.state.http.aclose()
        await app.state.redis.aclose()
        logger.info("gateway stopped", extra={"event": "shutdown"})


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_body(),
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body: dict[str, object] = {"detail": detail}
        else:
            body = {
                "detail": {
                    "code": _HTTP_STATUS_CODES.get(exc.status_code, "HTTP_ERROR"),
                    "message": str(detail),
                }
            }
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

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
        title="Virtual Wallet API Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "prod" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env != "prod" else None,
    )

    app.state.settings = settings
    app.state.routes = build_routes(settings)

    # Order matters: CORS must wrap everything so error responses also carry
    # the required headers. (Last added middleware is outermost.)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Request-Id",
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
    )

    _register_exception_handlers(app)
    app.include_router(health_router)

    @app.api_route(
        f"{settings.api_prefix}/{{path:path}}",
        methods=_PROXIED_METHODS,
        include_in_schema=False,
    )
    async def _proxy(request: Request) -> Response:
        return await proxy_request(request)

    return app


app = create_app()
