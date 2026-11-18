"""JWT verification behaviour at the gateway edge."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import httpx
import pytest
import respx

WALLET_URL = "http://wallet:8000/accounts"
ACCOUNT_BODY = {"accounts": [{"id": "acc_1", "balance": "10.00"}]}


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ({}, "AUTH_REQUIRED"),
        ({"Authorization": ""}, "AUTH_REQUIRED"),
        ({"Authorization": "   "}, "AUTH_REQUIRED"),
        ({"Authorization": "Basic dXNlcjpwYXNz"}, "INVALID_TOKEN"),
        ({"Authorization": "Bearer"}, "INVALID_TOKEN"),
        ({"Authorization": "Bearer    "}, "INVALID_TOKEN"),
    ],
)
async def test_missing_or_malformed_authorization_is_401(
    client, headers: dict[str, str], expected_code: str
) -> None:
    response = await client.get("/api/v1/wallet/accounts", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == expected_code
    assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"iss": "someone-else"}, "wrong issuer"),
        ({"typ": "refresh"}, "refresh token"),
        ({"sub": "not-a-uuid"}, "bad subject"),
        ({"ver": -1}, "negative version"),
        ({"jti": ""}, "missing jti"),
        ({"exp": None}, "missing exp"),
        ({"iat": None}, "missing iat"),
        ({"sub": None}, "missing sub"),
    ],
)
async def test_invalid_claims_are_rejected(
    client, make_token: Callable[..., str], overrides: dict, label: str
) -> None:
    token = make_token(**overrides)
    response = await client.get(
        "/api/v1/wallet/accounts", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401, label
    assert response.json()["detail"]["code"] == "INVALID_TOKEN", label


async def test_token_signed_with_another_secret_is_rejected(
    client, make_token: Callable[..., str]
) -> None:
    token = make_token(key="a-completely-different-secret-0123456789abcdef")
    response = await client.get(
        "/api/v1/wallet/accounts", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_TOKEN"


async def test_structurally_invalid_token_is_rejected(client) -> None:
    response = await client.get(
        "/api/v1/wallet/accounts", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_TOKEN"


async def test_alg_none_token_is_rejected(client) -> None:
    token = (
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
        "eyJzdWIiOiIxMTExMTExMS0yMjIyLTMzMzMtNDQ0NC01NTU1NTU1NTU1NTUiLCJpc3MiOiJ2aXJ0dWFs"
        "LXdhbGxldC1hdXRoIiwiaWF0IjoxNzAwMDAwMDAwLCJleHAiOjQwMDAwMDAwMDAsInR5cCI6ImFjY2Vz"
        "cyIsImp0aSI6ImoiLCJ2ZXIiOjB9."
    )
    response = await client.get(
        "/api/v1/wallet/accounts", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


@respx.mock
async def test_valid_token_is_forwarded_with_verified_identity(
    client, make_token: Callable[..., str]
) -> None:
    user_id = uuid.uuid4()
    route = respx.get(WALLET_URL).mock(return_value=httpx.Response(200, json=ACCOUNT_BODY))
    token = make_token(sub=str(user_id), ver=7)

    response = await client.get(
        "/api/v1/wallet/accounts", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json() == ACCOUNT_BODY
    forwarded = route.calls[0].request
    assert forwarded.headers["x-user-id"] == str(user_id)
    assert forwarded.headers["x-token-version"] == "7"
    assert "authorization" not in forwarded.headers


@respx.mock
async def test_client_cannot_spoof_identity_headers(
    client, make_token: Callable[..., str]
) -> None:
    real_user = uuid.uuid4()
    route = respx.get(WALLET_URL).mock(return_value=httpx.Response(200, json=ACCOUNT_BODY))

    response = await client.get(
        "/api/v1/wallet/accounts",
        headers={
            "Authorization": f"Bearer {make_token(sub=str(real_user))}",
            "X-User-Id": str(uuid.uuid4()),
            "X-Internal-Role": "admin",
            "X-Token-Version": "99",
            "X-Forwarded-For": "203.0.113.9",
        },
    )

    assert response.status_code == 200
    forwarded = route.calls[0].request.headers
    assert forwarded["x-user-id"] == str(real_user)
    assert forwarded["x-token-version"] == "0"
    assert "x-internal-role" not in forwarded
    assert "x-forwarded-for" not in forwarded


@respx.mock
async def test_token_within_leeway_window_is_accepted(
    client, make_token: Callable[..., str]
) -> None:
    """Expiry is enforced with a 30 s clock-skew allowance."""
    respx.get(WALLET_URL).mock(return_value=httpx.Response(200, json={}))
    token = make_token(exp=int(time.time()) - 5)
    response = await client.get(
        "/api/v1/wallet/accounts", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


async def test_expired_token_does_not_reach_upstream(
    client, make_token: Callable[..., str]
) -> None:
    with respx.mock(assert_all_called=False) as router:
        wallet = router.get(WALLET_URL).mock(return_value=httpx.Response(200))
        response = await client.get(
            "/api/v1/wallet/accounts",
            headers={"Authorization": f"Bearer {make_token(exp=int(time.time()) - 60)}"},
        )
    assert response.status_code == 401
    assert not wallet.called
