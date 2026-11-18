"""HTTP contract of the Auth Service, with the service layer faked.

These tests pin the wire format (status codes, response bodies, error
envelopes) without needing PostgreSQL. Real SQL is covered by
``tests/integration``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import jwt
import pytest
from httpx import AsyncClient

from app.api.routes import auth as auth_routes
from app.core.security import IssuedToken
from app.errors import (
    account_inactive,
    auth_required,
    email_already_registered,
    identity_not_found,
    invalid_credentials,
)
from app.models.user import User

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
PASSWORD = "correct-horse-battery"


def _make_user(*, is_active: bool = True) -> User:
    user = User(email="ada@example.com", password_hash="$2b$04$not-used-in-these-tests")
    user.id = USER_ID
    user.created_at = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    user.is_active = is_active
    user.is_verified = False
    user.token_version = 0
    return user


class FakeAuthService:
    """Stands in for ``AuthService``; each test configures the class state."""

    error: Exception | None = None
    login_result: tuple[User, IssuedToken] | None = None
    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def register(self, *, email: str, password: str) -> User:
        self.calls.append(("register", {"email": email, "password": password}))
        if self.error is not None:
            raise self.error
        return _make_user()

    async def login(self, *, email: str, password: str) -> tuple[User, IssuedToken]:
        self.calls.append(("login", {"email": email, "password": password}))
        if self.error is not None:
            raise self.error
        assert self.login_result is not None, "test must configure login_result"
        return self.login_result

    async def get_user(self, user_id: uuid.UUID) -> User:
        self.calls.append(("get_user", {"user_id": user_id}))
        if self.error is not None:
            raise self.error
        return _make_user()


@pytest.fixture(autouse=True)
def fake_service(monkeypatch: pytest.MonkeyPatch) -> type[FakeAuthService]:
    FakeAuthService.error = None
    FakeAuthService.login_result = None
    FakeAuthService.calls = []
    monkeypatch.setattr(auth_routes, "AuthService", FakeAuthService)
    return FakeAuthService


# ---------------------------------------------------------------------------
# /auth/register
# ---------------------------------------------------------------------------


async def test_register_returns_201_without_a_token(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert response.status_code == 201
    body = response.json()
    # Contract: registration never authenticates the caller. The client must
    # call /auth/login with the credentials it just submitted.
    assert "access_token" not in body
    assert set(body) == {"id", "email", "is_active", "is_verified", "created_at"}
    assert body["email"] == "ada@example.com"
    assert body["is_active"] is True
    assert body["is_verified"] is False
    assert response.headers["x-request-id"]


async def test_register_passes_the_normalised_email_to_the_service(
    client: AsyncClient, fake_service: type[FakeAuthService]
) -> None:
    response = await client.post(
        "/auth/register", json={"email": "  Ada@Example.com  ", "password": PASSWORD}
    )

    assert response.status_code == 201
    assert fake_service.calls == [("register", {"email": "Ada@example.com", "password": PASSWORD})]


async def test_duplicate_email_is_409(
    client: AsyncClient, fake_service: type[FakeAuthService]
) -> None:
    fake_service.error = email_already_registered()

    response = await client.post(
        "/auth/register", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_short_password_is_422_and_never_echoed(client: AsyncClient) -> None:
    submitted = "short"

    response = await client.post(
        "/auth/register", json={"email": "ada@example.com", "password": submitted}
    )

    assert response.status_code == 422
    # FastAPI echoes invalid input by default; that would print a password into
    # the response body and any client-side log.
    assert submitted not in response.text


async def test_validation_errors_never_contain_the_password(client: AsyncClient) -> None:
    submitted = "x" * 73

    response = await client.post(
        "/auth/register", json={"email": "ada@example.com", "password": submitted}
    )

    assert response.status_code == 422
    assert submitted not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "password"]
    assert "input" not in response.json()["detail"][0]


async def test_non_object_body_is_not_echoed(client: AsyncClient) -> None:
    """A body of the wrong *shape* must not be reflected either.

    Pydantic's ``model_attributes_type`` error includes the whole payload in
    ``input``; for an array or a bare string that payload may be a password.
    """
    response = await client.post(
        "/auth/register",
        content='["ada@example.com", "correct-horse-battery"]',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert "correct-horse-battery" not in response.text
    assert all("input" not in error for error in response.json()["detail"])


async def test_bare_string_body_is_not_echoed(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        content='"correct-horse-battery"',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert "correct-horse-battery" not in response.text


async def test_unknown_field_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        json={"email": "ada@example.com", "password": PASSWORD, "is_admin": True},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /auth/login
# ---------------------------------------------------------------------------


async def test_login_returns_access_token(
    client: AsyncClient, fake_service: type[FakeAuthService]
) -> None:
    issued = IssuedToken(token="signed.jwt.value", expires_at=1_800, expires_in=1800)
    fake_service.login_result = (_make_user(), issued)

    response = await client.post(
        "/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "signed.jwt.value",
        "token_type": "bearer",
        "expires_in": 1800,
    }


async def test_login_errors_are_uniform(
    client: AsyncClient, fake_service: type[FakeAuthService]
) -> None:
    fake_service.error = invalid_credentials()

    response = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_CREDENTIALS"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_inactive_account_is_403(
    client: AsyncClient, fake_service: type[FakeAuthService]
) -> None:
    fake_service.error = account_inactive()

    response = await client.post(
        "/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ACCOUNT_INACTIVE"


async def test_login_rejects_empty_password(client: AsyncClient) -> None:
    response = await client.post("/auth/login", json={"email": "ada@example.com", "password": ""})
    assert response.status_code == 422
    assert "input" not in response.json()["detail"][0]


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------


async def test_me_requires_the_identity_header(client: AsyncClient) -> None:
    response = await client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_me_rejects_a_malformed_identity_header(client: AsyncClient) -> None:
    response = await client.get("/auth/me", headers={"X-User-Id": "not-a-uuid"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


async def test_me_returns_the_user_for_the_injected_identity(
    client: AsyncClient, fake_service: type[FakeAuthService]
) -> None:
    response = await client.get("/auth/me", headers={"X-User-Id": str(USER_ID)})

    assert response.status_code == 200
    assert response.json()["id"] == str(USER_ID)
    assert fake_service.calls == [("get_user", {"user_id": USER_ID})]


async def test_me_does_not_accept_a_bearer_token(client: AsyncClient) -> None:
    """Identity is established by the gateway only (ADR-0004).

    A token that the service did not verify must never be treated as identity.
    """
    token = jwt.encode({"sub": str(USER_ID)}, "x" * 32, algorithm="HS256")
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_deleted_identity_is_401(
    client: AsyncClient, fake_service: type[FakeAuthService]
) -> None:
    fake_service.error = identity_not_found()

    response = await client.get("/auth/me", headers={"X-User-Id": str(USER_ID)})
    assert response.status_code == 401
    # RFC 7235: a 401 must advertise a challenge.
    assert response.headers["www-authenticate"] == "Bearer"


async def test_inactive_identity_is_403(
    client: AsyncClient, fake_service: type[FakeAuthService]
) -> None:
    fake_service.error = account_inactive()

    response = await client.get("/auth/me", headers={"X-User-Id": str(USER_ID)})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


async def test_unknown_auth_route_is_404_envelope(client: AsyncClient) -> None:
    response = await client.get("/auth/logout")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


def test_missing_identity_uses_the_shared_envelope() -> None:
    assert auth_required().to_body() == {
        "detail": {"code": "AUTH_REQUIRED", "message": "Authentication required."}
    }
