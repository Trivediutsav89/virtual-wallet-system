"""Liveness and readiness endpoints.

``/healthz`` answers for the process only (no dependencies) so it can be used
as a container liveness probe.

``/readyz`` probes Redis (required for rate limiting) and reports upstream
reachability. Redis down => 503. An upstream being down is reported but does
not fail readiness, because the gateway can still serve the remaining services.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

logger = logging.getLogger("gateway.health")

router = APIRouter(tags=["health"])

_PROBE_TIMEOUT_SECONDS = 2.0


@router.get("/healthz", summary="Liveness probe")
async def healthz(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {"status": "ok", "service": settings.app_name, "env": settings.app_env}


async def _probe_upstream(client: httpx.AsyncClient, base_url: str) -> bool:
    try:
        response = await client.get(f"{base_url}/healthz", timeout=_PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


@router.get("/readyz", summary="Readiness probe")
async def readyz(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    client: httpx.AsyncClient = request.app.state.http
    redis = request.app.state.redis

    checks: dict[str, str] = {}

    try:
        await redis.ping()
        checks["redis"] = "ok"
    except (RedisError, OSError) as exc:
        logger.warning("readiness: redis unreachable: %s", exc)
        checks["redis"] = "unavailable"

    upstreams = list(settings.upstream_urls.items())
    results = await asyncio.gather(
        *(_probe_upstream(client, url) for _, url in upstreams),
        return_exceptions=False,
    )
    for (name, _), healthy in zip(upstreams, results, strict=True):
        checks[name] = "ok" if healthy else "unavailable"

    ready = checks["redis"] == "ok"
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ok" if ready else "unavailable", "checks": checks},
    )
