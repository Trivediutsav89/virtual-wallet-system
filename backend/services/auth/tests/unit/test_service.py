"""Business logic with a fake session; no PostgreSQL required.

The SQL itself is exercised in ``tests/integration``. Here we pin the
decisions: hashing, transaction boundaries, error mapping, and the rule that
registration never fans out to other services.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app import services as services_package
from app.config import Settings
from app.core.security import hash_password
from app.errors import AppError
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services import auth_service as auth_service_module
from app.services.auth_service import AuthService

PASSWORD = "correct-horse-battery"
PASSWORD_HASH = hash_password(PASSWORD, 4)


class FakeSession:
    """The subset of ``AsyncSession`` that ``AuthService`` touches."""

    def __init__(self, *, flush_error: Exception | None = None) -> None:
        self.flush_error = flush_error
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error
        for instance in self.added:
            if getattr(instance, "id", None) is None:
                instance.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, instance: Any) -> None:
        self.refreshes += 1
        if instance.created_at is None:
            instance.created_at = datetime.now(UTC)


def _make_user(*, is_active: bool = True, password_hash: str = PASSWORD_HASH) -> User:
    user = User(email="ada@example.com", password_hash=password_hash)
    user.id = uuid.uuid4()
    user.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    user.is_active = is_active
    user.is_verified = False
    user.token_version = 2
    return user


def _patch_lookup(monkeypatch: pytest.MonkeyPatch, user: User | None) -> list[str]:
    """Replace the repository lookup and record the emails it was asked for."""
    seen: list[str] = []

    async def fake_get_by_email(self: UserRepository, email: str) -> User | None:
        seen.append(email)
        return user

    monkeypatch.setattr(UserRepository, "get_by_email", fake_get_by_email)
    return seen


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


async def test_register_hashes_the_password_before_storing(settings: Settings) -> None:
    session = FakeSession()
    service = AuthService(session, settings)  # type: ignore[arg-type]

    user = await service.register(email="ada@example.com", password=PASSWORD)

    assert user.password_hash.startswith("$2b$")
    assert PASSWORD not in user.password_hash
    assert user.email == "ada@example.com"
    assert session.commits == 1
    assert session.rollbacks == 0
    assert session.refreshes == 1


async def test_register_surfaces_duplicate_email_as_409(settings: Settings) -> None:
    error = IntegrityError("INSERT INTO users", {}, Exception("duplicate key"))
    session = FakeSession(flush_error=error)
    service = AuthService(session, settings)  # type: ignore[arg-type]

    with pytest.raises(AppError) as excinfo:
        await service.register(email="ada@example.com", password=PASSWORD)

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "EMAIL_ALREADY_REGISTERED"
    # The transaction must not be left open after a failed insert.
    assert session.rollbacks == 1
    assert session.commits == 0


async def test_register_never_calls_other_services() -> None:
    """ADR-0001/0004: registration has no fan-out, so no distributed commit."""
    tree = ast.parse(inspect.getsource(auth_service_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"httpx", "requests", "aiohttp", "urllib3", "grpc"}
    assert imported & forbidden == set()
    assert set(dir(services_package)) >= {"auth_service"}


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


async def test_login_issues_a_valid_token_and_records_the_login(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _make_user()
    _patch_lookup(monkeypatch, user)
    session = FakeSession()
    service = AuthService(session, settings)  # type: ignore[arg-type]

    returned_user, token = await service.login(email="ada@example.com", password=PASSWORD)

    assert returned_user is user
    assert token.expires_in == 1800
    assert user.last_login_at is not None
    assert user.last_login_at.tzinfo is not None
    assert session.commits == 1


async def test_login_passes_the_email_to_the_repository(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normalisation lives in the repository, next to the SQL that uses it."""
    user = _make_user()
    seen = _patch_lookup(monkeypatch, user)
    service = AuthService(FakeSession(), settings)  # type: ignore[arg-type]

    await service.login(email="  ADA@Example.com ", password=PASSWORD)

    assert seen == ["  ADA@Example.com "]


async def test_login_with_unknown_email_is_401(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lookup(monkeypatch, None)
    service = AuthService(FakeSession(), settings)  # type: ignore[arg-type]

    with pytest.raises(AppError) as excinfo:
        await service.login(email="nobody@example.com", password=PASSWORD)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "INVALID_CREDENTIALS"


async def test_unknown_email_still_costs_a_bcrypt_verification(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, response timing reveals whether an account exists."""
    _patch_lookup(monkeypatch, None)
    verified: list[tuple[str, str]] = []

    async def fake_verify(password: str, password_hash: str) -> bool:
        verified.append((password, password_hash))
        return False

    monkeypatch.setattr(auth_service_module, "verify_password_async", fake_verify)
    service = AuthService(FakeSession(), settings)  # type: ignore[arg-type]

    with pytest.raises(AppError):
        await service.login(email="nobody@example.com", password=PASSWORD)

    assert len(verified) == 1
    assert verified[0][1].startswith("$2b$")


async def test_wrong_password_is_401_and_does_not_commit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lookup(monkeypatch, _make_user())
    session = FakeSession()
    service = AuthService(session, settings)  # type: ignore[arg-type]

    with pytest.raises(AppError) as excinfo:
        await service.login(email="ada@example.com", password="wrong-password")

    assert excinfo.value.code == "INVALID_CREDENTIALS"
    assert session.commits == 0


async def test_inactive_account_is_403_only_after_password_check(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lookup(monkeypatch, _make_user(is_active=False))
    session = FakeSession()
    service = AuthService(session, settings)  # type: ignore[arg-type]

    with pytest.raises(AppError) as excinfo:
        await service.login(email="ada@example.com", password=PASSWORD)

    assert excinfo.value.status_code == 403
    assert excinfo.value.code == "ACCOUNT_INACTIVE"
    assert session.commits == 0


async def test_wrong_password_on_inactive_account_is_still_401(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never confirm that a disabled account exists to someone without it."""
    _patch_lookup(monkeypatch, _make_user(is_active=False))
    service = AuthService(FakeSession(), settings)  # type: ignore[arg-type]

    with pytest.raises(AppError) as excinfo:
        await service.login(email="ada@example.com", password="wrong-password")

    assert excinfo.value.code == "INVALID_CREDENTIALS"


# ---------------------------------------------------------------------------
# get_user
# ---------------------------------------------------------------------------


async def test_get_user_returns_the_matching_user(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _make_user()
    seen: list[uuid.UUID] = []

    async def fake_get_by_id(self: UserRepository, user_id: uuid.UUID) -> User | None:
        seen.append(user_id)
        return user

    monkeypatch.setattr(UserRepository, "get_by_id", fake_get_by_id)
    service = AuthService(FakeSession(), settings)  # type: ignore[arg-type]

    assert await service.get_user(user.id) is user
    assert seen == [user.id]


async def test_get_user_with_unknown_id_is_401(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_by_id(self: UserRepository, user_id: uuid.UUID) -> User | None:
        return None

    monkeypatch.setattr(UserRepository, "get_by_id", fake_get_by_id)
    service = AuthService(FakeSession(), settings)  # type: ignore[arg-type]

    with pytest.raises(AppError) as excinfo:
        await service.get_user(uuid.uuid4())

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "AUTH_REQUIRED"


async def test_get_user_with_deactivated_account_is_403(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _make_user(is_active=False)

    async def fake_get_by_id(self: UserRepository, user_id: uuid.UUID) -> User | None:
        return user

    monkeypatch.setattr(UserRepository, "get_by_id", fake_get_by_id)
    service = AuthService(FakeSession(), settings)  # type: ignore[arg-type]

    with pytest.raises(AppError) as excinfo:
        await service.get_user(user.id)

    assert excinfo.value.code == "ACCOUNT_INACTIVE"
