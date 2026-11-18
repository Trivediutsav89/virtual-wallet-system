"""Request id propagation and access logging."""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import TYPE_CHECKING

from starlette.datastructures import Headers, MutableHeaders

if TYPE_CHECKING:  # pragma: no cover
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("gateway.access")

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HEADER = "x-request-id"


def new_request_id() -> str:
    return uuid.uuid4().hex


def sanitise_request_id(value: str | None) -> str | None:
    """Accept a client-supplied id only if it is short and boring.

    Request ids end up in logs, so arbitrary client input is rejected rather
    than escaped.
    """
    if value and _REQUEST_ID_PATTERN.match(value):
        return value
    return None


class RequestContextMiddleware:
    """Assign a request id, expose it on ``request.state``, log the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = sanitise_request_id(headers.get(_HEADER)) or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id

        method = scope.get("method", "")
        path = scope.get("path", "")
        client = scope.get("client")
        client_host = client[0] if client else "-"

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = MutableHeaders(scope=message)
                response_headers[_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "%s %s %s",
                method,
                path,
                status_code,
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "status": status_code,
                    "duration_ms": duration_ms,
                    "client": client_host,
                },
            )
