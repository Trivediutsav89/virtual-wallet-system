"""Redis-backed fixed-window rate limiting.

A fixed window is deliberate: one ``INCR`` plus one ``EXPIRE`` per request,
trivially understood, and good enough to stop credential stuffing. It has the
classic boundary-burst weakness, which is documented and accepted (ADR-0004).

The limiter fails **open**: if Redis is unreachable, requests proceed and a
warning is logged. A cache outage must not take the API down.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import RateLimit, Settings, parse_rate_limit

logger = logging.getLogger("gateway.ratelimit")

KEY_PREFIX = "rl"


@dataclass(frozen=True, slots=True)
class LimitState:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int
    retry_after: int


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        default_limit: RateLimit | None = None,
        auth_limit: RateLimit | None = None,
    ) -> None:
        self._redis = redis
        self._enabled = settings.rate_limit_enabled
        self._default = default_limit or parse_rate_limit(settings.rate_limit_default)
        self._auth = auth_limit or parse_rate_limit(settings.rate_limit_auth)
        self._clock = clock

    def quota_for(self, downstream_path: str) -> tuple[str, RateLimit]:
        """Return ``(scope, quota)`` for a downstream path."""
        if downstream_path.startswith("/auth"):
            return "auth", self._auth
        return "default", self._default

    async def hit(self, identifier: str, scope: str, quota: RateLimit) -> LimitState:
        now = int(self._clock())
        window_index = now // quota.window_seconds
        reset_at = (window_index + 1) * quota.window_seconds

        if not self._enabled:
            return LimitState(
                allowed=True,
                limit=quota.limit,
                remaining=quota.limit,
                reset_at=reset_at,
                retry_after=0,
            )

        key = f"{KEY_PREFIX}:{scope}:{identifier}:{window_index}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, quota.window_seconds + 1)
                count, _ = await pipe.execute()
        except RedisError as exc:
            logger.warning(
                "rate limiter unavailable, failing open: %s",
                exc,
                extra={"event": "rate_limit_fail_open"},
            )
            return LimitState(
                allowed=True,
                limit=quota.limit,
                remaining=quota.limit,
                reset_at=reset_at,
                retry_after=0,
            )

        current = int(count)
        allowed = current <= quota.limit
        return LimitState(
            allowed=allowed,
            limit=quota.limit,
            remaining=max(0, quota.limit - current),
            reset_at=reset_at,
            retry_after=max(1, reset_at - now) if not allowed else 0,
        )
