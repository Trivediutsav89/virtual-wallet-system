"""Profile business logic with a fake session; no PostgreSQL required."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.profile import Profile
from app.repositories.profile_repository import ProfileRepository
from app.schemas.profile import ProfilePatchRequest
from app.services.profile_service import ProfileService

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _instance: Any) -> None:
        self.refreshes += 1


def _make_profile() -> Profile:
    profile = Profile(
        user_id=USER_ID,
        display_name="Old name",
        phone_number="+91 00000",
    )
    profile.id = uuid.uuid4()
    profile.created_at = datetime(2026, 11, 23, tzinfo=UTC)
    profile.updated_at = datetime(2026, 11, 23, tzinfo=UTC)
    return profile


def _patch_repository(
    monkeypatch: pytest.MonkeyPatch, profile: Profile | None
) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []

    async def fake_get_or_create(
        self: ProfileRepository, user_id: uuid.UUID
    ) -> Profile | None:
        seen.append(user_id)
        return profile

    monkeypatch.setattr(ProfileRepository, "get_or_create", fake_get_or_create)
    return seen


async def test_get_or_create_uses_trusted_user_id_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _make_profile()
    seen = _patch_repository(monkeypatch, profile)
    session = FakeSession()

    result = await ProfileService(cast(AsyncSession, session)).get_or_create(USER_ID)

    assert result is profile
    assert seen == [USER_ID]
    assert session.commits == 1
    assert session.refreshes == 1


async def test_missing_profile_after_create_is_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_repository(monkeypatch, None)
    session = FakeSession()

    with pytest.raises(AppError) as excinfo:
        await ProfileService(cast(AsyncSession, session)).get_or_create(USER_ID)

    assert excinfo.value.status_code == 500
    assert excinfo.value.code == "INTERNAL_ERROR"
    assert session.commits == 0


async def test_patch_applies_only_supplied_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _make_profile()
    _patch_repository(monkeypatch, profile)
    session = FakeSession()

    updated = await ProfileService(cast(AsyncSession, session)).update(
        USER_ID, ProfilePatchRequest(display_name="New name")
    )

    assert updated.display_name == "New name"
    assert updated.phone_number == "+91 00000"


async def test_patch_explicit_null_clears_a_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _make_profile()
    _patch_repository(monkeypatch, profile)
    session = FakeSession()

    updated = await ProfileService(cast(AsyncSession, session)).update(
        USER_ID, ProfilePatchRequest(phone_number=None)
    )

    assert updated.display_name == "Old name"
    assert updated.phone_number is None


async def test_patch_lazily_creates_a_missing_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _make_profile()
    seen = _patch_repository(monkeypatch, profile)
    session = FakeSession()

    await ProfileService(cast(AsyncSession, session)).update(
        USER_ID, ProfilePatchRequest(display_name="Created by patch")
    )

    assert seen == [USER_ID]
    assert profile.display_name == "Created by patch"
    assert session.commits >= 1
