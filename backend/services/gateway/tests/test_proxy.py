"""Proxy behaviour: routing, pass-through, header hygiene, upstream failures."""

from __future__ import annotations

import httpx
import pytest
import respx

WALLET_BASE = "http://wallet:8000"
AUTH_BASE = "http://auth:8000"


@respx.mock
async def test_public_route_needs_no_token(client) -> None:
    route = respx.post(f"{AUTH_BASE}/auth/register").mock(
        return_value=httpx.Response(201, json={"id": "u1", "email": "a@b.co"})
    )

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "a@b.co", "password": "correct-horse-battery"},
    )

    assert response.status_code == 201
    assert response.json()["email"] == "a@b.co"
    forwarded = route.calls[0].request
    assert "x-user-id" not in forwarded.headers
    assert "authorization" not in forwarded.headers


@respx.mock
async def test_public_route_tolerates_trailing_slash(client) -> None:
    respx.post(f"{AUTH_BASE}/auth/login/").mock(
        return_value=httpx.Response(200, json={"access_token": "t"})
    )
    response = await client.post("/api/v1/auth/login/", json={})
    assert response.status_code == 200


@respx.mock
async def test_non_public_auth_route_requires_token(client) -> None:
    me = respx.get(f"{AUTH_BASE}/auth/me").mock(return_value=httpx.Response(200, json={}))
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert not me.called


@respx.mock
async def test_status_body_and_query_are_passed_through(client, auth_headers) -> None:
    route = respx.get(f"{WALLET_BASE}/accounts").mock(
        return_value=httpx.Response(200, json={"page": 2})
    )

    response = await client.get("/api/v1/wallet/accounts?page=2&size=10", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"page": 2}
    assert route.calls[0].request.url.params["page"] == "2"
    assert route.calls[0].request.url.params["size"] == "10"


@respx.mock
async def test_upstream_error_status_is_preserved(client, auth_headers) -> None:
    respx.get(f"{WALLET_BASE}/accounts").mock(
        return_value=httpx.Response(422, json={"detail": [{"msg": "bad"}]})
    )
    response = await client.get("/api/v1/wallet/accounts", headers=auth_headers)
    assert response.status_code == 422
    assert response.json() == {"detail": [{"msg": "bad"}]}


async def test_unknown_service_is_404(client, auth_headers) -> None:
    response = await client.get("/api/v1/billing/invoices", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


@respx.mock
async def test_upstream_unreachable_is_502(client, auth_headers) -> None:
    respx.get(f"{WALLET_BASE}/accounts").mock(side_effect=httpx.ConnectError("refused"))
    response = await client.get("/api/v1/wallet/accounts", headers=auth_headers)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "UPSTREAM_UNAVAILABLE"


@respx.mock
async def test_upstream_timeout_is_504(client, auth_headers) -> None:
    respx.get(f"{WALLET_BASE}/accounts").mock(side_effect=httpx.ReadTimeout("slow"))
    response = await client.get("/api/v1/wallet/accounts", headers=auth_headers)
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "UPSTREAM_TIMEOUT"


@respx.mock
async def test_response_headers_are_sanitised(client, auth_headers) -> None:
    respx.get(f"{WALLET_BASE}/accounts").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True},
            headers={
                "X-Trace": "abc",
                "Connection": "keep-alive",
                "Keep-Alive": "timeout=5",
                "Content-Language": "en",
            },
        )
    )
    response = await client.get("/api/v1/wallet/accounts", headers=auth_headers)
    assert response.headers["x-trace"] == "abc"
    assert response.headers["content-language"] == "en"
    assert "connection" not in response.headers
    assert "keep-alive" not in response.headers
    assert response.json() == {"ok": True}


def test_response_header_filter_drops_framing_headers() -> None:
    """Upstreams must not be able to desynchronise the gateway's framing."""
    from starlette.datastructures import Headers

    from app.routing.proxy import build_response_headers

    upstream = Headers(
        raw=[
            (b"content-type", b"application/json"),
            (b"content-length", b"999"),
            (b"content-encoding", b"gzip"),
            (b"transfer-encoding", b"chunked"),
            (b"connection", b"close"),
            (b"x-ok", b"1"),
        ]
    )
    assert build_response_headers(upstream) == {
        "content-type": "application/json",
        "x-ok": "1",
    }


@respx.mock
async def test_request_id_is_generated_and_propagated(client, auth_headers) -> None:
    route = respx.get(f"{WALLET_BASE}/accounts").mock(return_value=httpx.Response(200, json={}))

    response = await client.get("/api/v1/wallet/accounts", headers=auth_headers)

    returned = response.headers["x-request-id"]
    assert len(returned) == 32
    assert route.calls[0].request.headers["x-request-id"] == returned


@respx.mock
async def test_valid_client_request_id_is_kept(client, auth_headers) -> None:
    route = respx.get(f"{WALLET_BASE}/accounts").mock(return_value=httpx.Response(200, json={}))
    response = await client.get(
        "/api/v1/wallet/accounts",
        headers={**auth_headers, "X-Request-Id": "trace-123"},
    )
    assert response.headers["x-request-id"] == "trace-123"
    assert route.calls[0].request.headers["x-request-id"] == "trace-123"


@respx.mock
async def test_hostile_request_id_is_replaced(client, auth_headers) -> None:
    respx.get(f"{WALLET_BASE}/accounts").mock(return_value=httpx.Response(200, json={}))
    response = await client.get(
        "/api/v1/wallet/accounts",
        headers={**auth_headers, "X-Request-Id": "bad id with spaces\nand newline"},
    )
    assert response.headers["x-request-id"] != "bad id with spaces\nand newline"
    assert len(response.headers["x-request-id"]) == 32


@respx.mock
async def test_cors_preflight_is_answered_by_the_gateway(client) -> None:
    response = await client.options(
        "/api/v1/wallet/accounts",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


async def test_unrouted_path_outside_api_prefix_returns_envelope(client) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "path",
    ["/api/v1/auth/login", "/api/v1/auth/register"],
)
async def test_public_paths_are_exactly_the_login_and_register_routes(
    client, path: str
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route(method="POST", url__startswith=AUTH_BASE).mock(
            return_value=httpx.Response(200, json={})
        )
        response = await client.post(path, json={})
    assert response.status_code == 200
