"""Generic reverse proxy: routing, identity injection, header hygiene.

The gateway owns no business logic. It resolves a public path to an upstream,
authenticates it (unless the route is public), and forwards it with a
sanitised header set.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from starlette.datastructures import Headers
from starlette.responses import Response

from app.config import Settings
from app.errors import bad_gateway, gateway_timeout, not_found, rate_limited
from app.middleware.rate_limit import RateLimiter
from app.security.jwt import Principal, authenticate

if TYPE_CHECKING:  # pragma: no cover
    from starlette.requests import Request

logger = logging.getLogger("gateway.proxy")

# RFC 7230 hop-by-hop headers plus anything the proxy must recompute.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Never forwarded from the client. `authorization` is dropped after
# verification: downstream services must not re-verify or re-use a token when
# the gateway has already established identity. `x-request-id` is always
# re-injected from the sanitised value, so the client copy must not survive.
_NEVER_FORWARD = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "content-length",
        "accept-encoding",
        "expect",
        "x-request-id",
    }
)

# Stripped before injection so a client cannot impersonate another user.
_IDENTITY_PREFIXES = ("x-user-", "x-internal-", "x-token-", "x-forwarded-")

_UNSAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "keep-alive",
    }
)


@dataclass(frozen=True, slots=True)
class Route:
    """A public prefix mapped to one upstream service.

    ``strip_prefix`` controls whether the public segment is removed before
    forwarding:

    * ``/auth/login``    -> ``/auth/login`` (Auth owns an ``/auth`` namespace)
    * ``/wallet/accounts`` -> ``/accounts`` (Wallet owns its root namespace)
    """

    name: str
    prefix: str
    upstream: str
    public_paths: frozenset[str] = frozenset()
    strip_prefix: bool = False

    def matches(self, path: str) -> bool:
        return path == self.prefix or path.startswith(f"{self.prefix}/")

    def is_public(self, path: str) -> bool:
        return path.rstrip("/") in self.public_paths

    def upstream_path(self, path: str) -> str:
        if not self.strip_prefix:
            return path
        remainder = path[len(self.prefix) :]
        return remainder or "/"


def build_routes(settings: Settings) -> tuple[Route, ...]:
    return (
        Route(
            name="auth",
            prefix="/auth",
            upstream=settings.auth_service_url,
            # Only these two are reachable without a token. Everything else
            # under /auth (e.g. /auth/me) requires authentication.
            public_paths=frozenset({"/auth/register", "/auth/login"}),
        ),
        Route(
            name="profile",
            prefix="/profile",
            upstream=settings.profile_service_url,
            strip_prefix=True,
        ),
        Route(
            name="wallet",
            prefix="/wallet",
            upstream=settings.wallet_service_url,
            strip_prefix=True,
        ),
    )


def match_route(routes: tuple[Route, ...], path: str) -> Route | None:
    """Longest-prefix match, with segment boundaries enforced."""
    best: Route | None = None
    for route in routes:
        if route.matches(path) and (best is None or len(route.prefix) > len(best.prefix)):
            best = route
    return best


def downstream_path(api_prefix: str, captured: str) -> str:
    """Turn the captured path parameter into a service path beginning with ``/``."""
    clean_prefix = "/" + api_prefix.strip("/")
    path = "/" + captured.lstrip("/")
    if path == "/":
        return "/"
    # Guard against a caller reaching this helper with the prefix still attached.
    if clean_prefix != "/" and (path == clean_prefix or path.startswith(f"{clean_prefix}/")):
        remainder = path[len(clean_prefix) :]
        return remainder or "/"
    return path


def client_identifier(request: Request, *, trust_forwarded_for: bool) -> str:
    """Best-effort client key for rate limiting.

    ``X-Forwarded-For`` is client-controlled unless a trusted proxy overwrites
    it, so it is ignored unless explicitly enabled.
    """
    if trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def build_upstream_headers(
    incoming: Headers,
    *,
    principal: Principal | None,
    request_id: str,
) -> list[tuple[str, str]]:
    """Copy safe headers, drop spoofable ones, inject verified identity."""
    forwarded: list[tuple[str, str]] = []
    for key, value in incoming.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP or lowered in _NEVER_FORWARD:
            continue
        if lowered.startswith(_IDENTITY_PREFIXES):
            continue
        forwarded.append((key, value))

    forwarded.append(("x-request-id", request_id))
    if principal is not None:
        forwarded.append(("x-user-id", str(principal.user_id)))
        forwarded.append(("x-token-version", str(principal.token_version)))
        forwarded.append(("x-token-jti", principal.token_id))

    return forwarded


def build_response_headers(upstream_headers: Mapping[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in upstream_headers.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP or lowered in _UNSAFE_RESPONSE_HEADERS:
            continue
        headers[key] = value
    return headers


async def proxy_request(request: Request) -> Response:
    """Proxy the current request to the matched upstream."""
    settings: Settings = request.app.state.settings
    routes: tuple[Route, ...] = request.app.state.routes
    limiter: RateLimiter = request.app.state.rate_limiter
    client: httpx.AsyncClient = request.app.state.http
    request_id: str = getattr(request.state, "request_id", "-")

    path = downstream_path(settings.api_prefix, request.path_params.get("path", ""))
    route = match_route(routes, path)
    if route is None:
        raise not_found(f"No service is routed for {path!r}.")

    scope, quota = limiter.quota_for(path)
    limit_state = await limiter.hit(
        client_identifier(request, trust_forwarded_for=settings.trust_forwarded_for),
        scope,
        quota,
    )
    if not limit_state.allowed:
        raise rate_limited(
            limit=limit_state.limit,
            remaining=limit_state.remaining,
            reset_at=limit_state.reset_at,
            retry_after=limit_state.retry_after,
        )

    principal: Principal | None = None
    if not route.is_public(path):
        principal = authenticate(request.headers.get("authorization"), settings)

    headers = build_upstream_headers(
        request.headers,
        principal=principal,
        request_id=request_id,
    )
    body = await request.body()
    target = f"{route.upstream}{route.upstream_path(path)}"

    try:
        upstream_response = await client.request(
            method=request.method,
            url=target,
            params=request.url.query or None,
            content=body or None,
            headers=headers,
        )
    except httpx.TimeoutException as exc:
        logger.warning(
            "upstream timeout: %s %s", request.method, target, extra={"upstream": route.name}
        )
        raise gateway_timeout() from exc
    except httpx.RequestError as exc:
        logger.warning(
            "upstream unavailable: %s %s (%s)",
            request.method,
            target,
            exc,
            extra={"upstream": route.name},
        )
        raise bad_gateway() from exc

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=build_response_headers(upstream_response.headers),
    )
