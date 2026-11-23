"""HTTP contract of the Profile Service with the service layer faked."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.routes import profile as profile_routes
from app.db import get_session
from app.errors import auth_required
from app.main import app
from app.models.profile import Profile

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
HEADERS = {"X-User-Id": str(USER_ID)}


def _make_profile() -> Profile:
    profile = Profile(user_id=USER_ID, display_name="Ada", phone_number=None)
    profile.id = uuid.uuid4()
    profile.created_at = datetime(2026, 11, 23, tzinfo=UTC)
    profile.updated_at = datetime(2026, 11, 23, tzinfo=UTC)
    return profile


class FakeProfileService:
    error: Exception | None = None
    calls: ClassVar[list[tuple[str, Any]]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def get_or_create(self, user_id: uuid.UUID) -> Profile:
        self.calls.append(("get_or_create", user_id))
        if self.error is not None:
            raise self.error
        return _make_profile()

    async def update(self, user_id: uuid.UUID, patch: Any) -> Profile:
        self.calls.append(("update", (user_id, patch.model_dump(exclude_unset=True))))
        if self.error is not None:
            raise self.error
        profile = _make_profile()
        profile.display_name = patch.display_name
        return profile


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async def fake_session() -> AsyncIterator[Any]:
        yield object()

    app.dependency_overrides[get_session] = fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://profile") as http_client:
        yield http_client


@pytest.fixture(autouse=True)
def fake_service(monkeypatch: pytest.MonkeyPatch) -> type[FakeProfileService]:
    FakeProfileService.error = None
    FakeProfileService.calls = []
    monkeypatch.setattr(profile_routes, "ProfileService", FakeProfileService)
    return FakeProfileService


async def test_get_me_returns_profile(client: AsyncClient) -> None:
    response = await client.get("/me", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(USER_ID)
    assert body["display_name"] == "Ada"
    assert body["phone_number"] is None


async def test_get_me_requires_identity_header(client: AsyncClient) -> None:
    response = await client.get("/me")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


async def test_get_me_rejects_malformed_identity_header(client: AsyncClient) -> None:
    response = await client.get("/me", headers={"X-User-Id": "not-a-uuid"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


async def test_patch_me_applies_partial_update(client: AsyncClient) -> None:
    response = await client.patch(
        "/me", json={"display_name": "Grace"}, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Grace"


async def test_patch_me_rejects_unknown_fields(client: AsyncClient) -> None:
    response = await client.patch(
        "/me", json={"user_id": str(uuid.uuid4())}, headers=HEADERS
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "user_id"]
    assert str(uuid.uuid4()) not in response.text


async def test_patch_me_does_not_echo_input(client: AsyncClient) -> None:
    secret = "super-secret-display-name-value"
    response = await client.patch(
        "/me", json={"display_name": "x" * 101, "note": secret}, headers=HEADERS
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert all("input" not in error for error in response.json()["detail"])


async def test_service_errors_are_wrapped_without_leaking(
    client: AsyncClient, fake_service: type[FakeProfileService]
) -> None:
    fake_service.error = auth_required("Malformed identity header.")
    response = await client.get("/me", headers=HEADERS)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
