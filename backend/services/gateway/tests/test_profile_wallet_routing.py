"""Gateway coverage for the Day 2 Profile and Wallet routes.

The gateway routing code is unchanged: these tests prove that the existing
prefix stripping, identity injection and error mapping work for the new paths.
"""

from __future__ import annotations

import uuid

import httpx
import respx

PROFILE_BASE = "http://profile:8000"
WALLET_BASE = "http://wallet:8000"
USER_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
SPOOF_ID = uuid.UUID("88888888-8888-4888-8888-888888888888")


@respx.mock
async def test_profile_me_is_routed_with_verified_identity(client, make_token) -> None:
    route = respx.get(f"{PROFILE_BASE}/me").mock(
        return_value=httpx.Response(200, json={"user_id": str(USER_ID)})
    )

    response = await client.get(
        "/api/v1/profile/me",
        headers={
            "Authorization": f"Bearer {make_token(sub=str(USER_ID))}",
            "X-User-Id": str(SPOOF_ID),
            "X-Internal-Roles": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == str(USER_ID)
    forwarded = route.calls[0].request.headers
    assert forwarded["x-user-id"] == str(USER_ID)
    assert "x-internal-roles" not in forwarded
    assert "authorization" not in forwarded
    assert forwarded["x-request-id"] == response.headers["x-request-id"]


@respx.mock
async def test_profile_patch_is_routed_with_body(client, auth_headers) -> None:
    route = respx.patch(f"{PROFILE_BASE}/me").mock(
        return_value=httpx.Response(200, json={"display_name": "Ada"})
    )

    response = await client.patch(
        "/api/v1/profile/me",
        json={"display_name": "Ada"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert route.calls[0].request.content == b'{"display_name":"Ada"}'


@respx.mock
async def test_wallet_me_is_routed_to_root(client, auth_headers) -> None:
    route = respx.get(f"{WALLET_BASE}/me").mock(
        return_value=httpx.Response(200, json={"balance": "0.00", "currency": "INR"})
    )

    response = await client.get("/api/v1/wallet/me", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["balance"] == "0.00"
    assert route.called


@respx.mock
async def test_wallet_deposit_and_withdraw_are_routed(client, make_token) -> None:
    deposit = respx.post(f"{WALLET_BASE}/deposit").mock(
        return_value=httpx.Response(200, json={"balance": "500.00"})
    )
    withdraw = respx.post(f"{WALLET_BASE}/withdraw").mock(
        return_value=httpx.Response(200, json={"balance": "400.00"})
    )
    headers = {
        "Authorization": f"Bearer {make_token(sub=str(USER_ID))}",
        "X-User-Id": str(SPOOF_ID),
    }

    deposited = await client.post(
        "/api/v1/wallet/deposit", json={"amount": "500.00"}, headers=headers
    )
    withdrawn = await client.post(
        "/api/v1/wallet/withdraw", json={"amount": "100.00"}, headers=headers
    )

    assert deposited.json()["balance"] == "500.00"
    assert withdrawn.json()["balance"] == "400.00"
    assert deposit.calls[0].request.headers["x-user-id"] == str(USER_ID)
    assert withdraw.calls[0].request.headers["x-user-id"] == str(USER_ID)


@respx.mock
async def test_insufficient_funds_status_is_preserved(client, auth_headers) -> None:
    respx.post(f"{WALLET_BASE}/withdraw").mock(
        return_value=httpx.Response(
            409,
            json={"detail": {"code": "INSUFFICIENT_FUNDS", "message": "nope"}},
        )
    )

    response = await client.post(
        "/api/v1/wallet/withdraw", json={"amount": "999.00"}, headers=auth_headers
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INSUFFICIENT_FUNDS"


async def test_profile_and_wallet_require_authentication(client) -> None:
    for url in ("/api/v1/profile/me", "/api/v1/wallet/me", "/api/v1/wallet/deposit"):
        response = await client.get(url) if url.endswith("/me") else await client.post(url, json={})
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


@respx.mock
async def test_wallet_upstream_failure_is_502(client, auth_headers) -> None:
    respx.get(f"{WALLET_BASE}/me").mock(side_effect=httpx.ConnectError("refused"))

    response = await client.get("/api/v1/wallet/me", headers=auth_headers)

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "UPSTREAM_UNAVAILABLE"
