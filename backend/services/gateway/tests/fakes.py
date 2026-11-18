"""Test doubles shared across the gateway suite."""

from __future__ import annotations

from redis.exceptions import RedisError


class BrokenRedis:
    """Minimal stand-in for a Redis outage.

    ``pipeline`` is a *sync* method on the real client that returns an async
    context manager, so raising synchronously here reproduces the same failure
    path the limiter must survive.
    """

    async def ping(self) -> bool:
        raise RedisError("connection refused")

    def pipeline(self, transaction: bool = True):
        raise RedisError("connection refused")
