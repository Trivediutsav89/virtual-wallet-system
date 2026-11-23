"""End-to-end Profile behaviour against PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.profile import Profile
from app.repositories.profile_repository import ProfileRepository

pytestmark = pytest.mark.integration

USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER_USER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
HEADERS = {"X-User-Id": str(USER_ID)}
OTHER_HEADERS = {"X-User-Id": str(OTHER_USER_ID)}


async def test_migration_head_is_applied(db_session: AsyncSession) -> None:
    version = await db_session.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0002_create_profiles"


async def test_user_id_is_unique(db_session: AsyncSession) -> None:
    first = Profile(user_id=USER_ID)
    db_session.add(first)
    await db_session.flush()
    db_session.add(Profile(user_id=USER_ID))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_get_me_lazily_creates_and_is_idempotent(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    first = await db_client.get("/me", headers=HEADERS)
    assert first.status_code == 200
    second = await db_client.get("/me", headers=HEADERS)
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["user_id"] == str(USER_ID)
    assert first.json()["display_name"] is None

    profiles = (await db_session.execute(select(Profile))).scalars().all()
    assert len(profiles) == 1


async def test_patch_updates_and_null_clears(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    assert (await db_client.get("/me", headers=HEADERS)).status_code == 200

    patched = await db_client.patch(
        "/me",
        json={"display_name": "Ada Lovelace", "phone_number": "+91 99999"},
        headers=HEADERS,
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Ada Lovelace"
    assert patched.json()["phone_number"] == "+91 99999"

    cleared = await db_client.patch(
        "/me", json={"phone_number": None}, headers=HEADERS
    )
    assert cleared.status_code == 200
    assert cleared.json()["display_name"] == "Ada Lovelace"
    assert cleared.json()["phone_number"] is None
    assert len((await db_session.execute(select(Profile))).scalars().all()) == 1


async def test_patch_lazily_creates_missing_profile(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await db_client.patch(
        "/me", json={"display_name": "Created by patch"}, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Created by patch"
    assert len((await db_session.execute(select(Profile))).scalars().all()) == 1


async def test_ownership_isolation(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    mine = await db_client.get("/me", headers=HEADERS)
    theirs = await db_client.get("/me", headers=OTHER_HEADERS)
    assert mine.status_code == theirs.status_code == 200
    assert mine.json()["id"] != theirs.json()["id"]
    assert mine.json()["user_id"] == str(USER_ID)
    assert theirs.json()["user_id"] == str(OTHER_USER_ID)

    await db_client.patch("/me", json={"display_name": "Mine"}, headers=HEADERS)
    other = (
        await db_session.execute(select(Profile).where(Profile.user_id == OTHER_USER_ID))
    ).scalar_one()
    assert other.display_name is None


async def test_concurrent_lazy_creation_is_idempotent(test_dsn: str) -> None:
    """Two independent sessions racing on the same user must not duplicate rows."""
    user_id = uuid.uuid4()

    async def create_once() -> Profile:
        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                sessionmaker = async_sessionmaker(bind=connection, expire_on_commit=False)
                async with sessionmaker() as session:
                    repo = ProfileRepository(session)
                    created = await repo.get_or_create(user_id)
                    assert created is not None
                    await session.commit()
                    return created
        finally:
            await engine.dispose()

    try:
        results = await asyncio.gather(create_once(), create_once())
        assert results[0].id == results[1].id
        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text("SELECT count(*) FROM profiles WHERE user_id = :uid"),
                        {"uid": user_id},
                    )
                ).scalar_one()
                assert rows == 1
        finally:
            await engine.dispose()
    finally:
        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM profiles WHERE user_id = :uid"), {"uid": user_id}
                )
        finally:
            await engine.dispose()
