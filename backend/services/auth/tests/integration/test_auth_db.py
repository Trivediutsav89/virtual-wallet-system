"""End-to-end Auth Service behaviour against PostgreSQL.

Runs only when ``AUTH_TEST_DATABASE_URL`` points at a reachable database; see
``tests/integration/conftest.py``. Every test is rolled back, but the migration
chain itself is applied once with ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import jwt
import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.user import User

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery"


async def _register(client: AsyncClient, email: str, password: str = PASSWORD) -> Response:
    return await client.post("/auth/register", json={"email": email, "password": password})


async def _users(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def test_migration_head_is_applied(db_session: AsyncSession) -> None:
    version = await db_session.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0001_create_users"


async def test_users_has_no_session_or_refresh_columns(db_session: AsyncSession) -> None:
    columns = set(
        (
            await db_session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'users'"
                )
            )
        ).scalars()
    )
    assert columns == {
        "id",
        "email",
        "password_hash",
        "is_active",
        "is_verified",
        "token_version",
        "last_login_at",
        "created_at",
        "updated_at",
    }


async def test_email_uniqueness_is_case_insensitive(db_session: AsyncSession) -> None:
    """The functional UNIQUE index must reject case-only email variants.

    ``pg_indexes`` renders the expression as ``lower((email)::text)``, so an
    exact DDL-string comparison is brittle. Normalise the definition, then
    prove the constraint with a direct insert that bypasses the service layer.
    """
    indexdefs = list(
        (
            await db_session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE tablename = 'users'")
            )
        ).scalars()
    )
    normalised = [
        "".join(indexdef.split())
        .replace("::text", "")
        .replace("((", "(")
        .replace("))", ")")
        for indexdef in indexdefs
    ]
    assert any(
        "CREATEUNIQUEINDEX" in definition and "lower(email)" in definition
        for definition in normalised
    ), indexdefs

    insert = text(
        "INSERT INTO users (email, password_hash) VALUES (:email, :password_hash)"
    )
    await db_session.execute(
        insert, {"email": "Case@Test.example", "password_hash": "not-a-real-hash"}
    )
    with pytest.raises(IntegrityError):
        await db_session.execute(
            insert, {"email": "case@test.example", "password_hash": "not-a-real-hash"}
        )
    await db_session.rollback()


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


async def test_register_persists_one_user_with_a_bcrypt_hash(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await _register(db_client, "ada@example.com")

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "ada@example.com"
    assert body["is_active"] is True
    assert body["is_verified"] is False
    assert body["created_at"]

    users = await _users(db_session)
    assert len(users) == 1
    stored = users[0]
    assert str(stored.id) == body["id"]
    assert stored.password_hash.startswith("$2b$")
    assert PASSWORD not in stored.password_hash
    assert stored.last_login_at is None


async def test_register_stores_the_trimmed_email(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await _register(db_client, "  Ada@Example.com  ")

    assert response.status_code == 201
    assert (await _users(db_session))[0].email == "Ada@example.com"


async def test_duplicate_email_is_409_and_does_not_create_a_second_row(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    assert (await _register(db_client, "ada@example.com")).status_code == 201

    duplicate = await _register(db_client, "ada@example.com", "another-password")

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert len(await _users(db_session)) == 1


async def test_duplicate_email_is_detected_across_case(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    assert (await _register(db_client, "ada@example.com")).status_code == 201

    duplicate = await _register(db_client, "ADA@EXAMPLE.COM")

    assert duplicate.status_code == 409
    assert len(await _users(db_session)) == 1


async def test_registered_password_is_not_accepted_with_a_different_value(
    db_client: AsyncClient,
) -> None:
    await _register(db_client, "ada@example.com")

    response = await db_client.post(
        "/auth/login", json={"email": "ada@example.com", "password": "not-the-password"}
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# login + me
# ---------------------------------------------------------------------------


async def test_login_issues_a_30_minute_token_with_minimal_claims(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    registration = await _register(db_client, "ada@example.com")
    user_id = registration.json()["id"]

    response = await db_client.post(
        "/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800

    payload = jwt.decode(
        body["access_token"],
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "sub", "iss", "typ", "jti"]},
    )
    assert set(payload) == {"sub", "iss", "iat", "exp", "jti", "typ", "ver"}
    assert payload["sub"] == user_id
    assert payload["typ"] == "access"
    assert payload["exp"] - payload["iat"] == 1800

    users = await _users(db_session)
    assert users[0].last_login_at is not None
    assert payload["ver"] == users[0].token_version


async def test_login_is_case_insensitive(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    assert (await _register(db_client, "ada@example.com")).status_code == 201

    response = await db_client.post(
        "/auth/login", json={"email": "ADA@EXAMPLE.COM", "password": PASSWORD}
    )

    assert response.status_code == 200
    payload = jwt.decode(
        response.json()["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    assert payload["sub"] == str((await _users(db_session))[0].id)


async def test_login_with_unknown_email_is_401_and_creates_nothing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await db_client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_CREDENTIALS"
    assert await _users(db_session) == []


async def test_me_returns_the_authenticated_user(db_client: AsyncClient) -> None:
    registration = await _register(db_client, "ada@example.com")
    user_id = registration.json()["id"]

    response = await db_client.get("/auth/me", headers={"X-User-Id": user_id})

    assert response.status_code == 200
    assert response.json() == registration.json()


async def test_me_with_unknown_identity_is_401(db_client: AsyncClient) -> None:
    response = await db_client.get("/auth/me", headers={"X-User-Id": str(uuid.uuid4())})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


async def test_deactivated_account_cannot_log_in_or_use_me(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    registration = await _register(db_client, "ada@example.com")
    user_id = registration.json()["id"]

    user = await db_session.scalar(select(User).where(User.id == uuid.UUID(user_id)))
    assert user is not None
    user.is_active = False
    await db_session.commit()

    login = await db_client.post(
        "/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )
    assert login.status_code == 403
    assert login.json()["detail"]["code"] == "ACCOUNT_INACTIVE"

    me = await db_client.get("/auth/me", headers={"X-User-Id": user_id})
    assert me.status_code == 403


async def test_token_version_reflects_the_database_value(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await _register(db_client, "ada@example.com")
    user = (await _users(db_session))[0]
    user.token_version = 7
    user.last_login_at = datetime.now(UTC)
    await db_session.commit()

    response = await db_client.post(
        "/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )
    payload = jwt.decode(response.json()["access_token"], settings.jwt_secret, algorithms=["HS256"])
    assert payload["ver"] == 7
