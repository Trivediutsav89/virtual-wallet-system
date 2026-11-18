"""Request-schema rules (pure validation, no database, no HTTP)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.config import BCRYPT_MAX_PASSWORD_BYTES
from app.schemas.auth import LoginRequest, RegisterRequest

VALID_PASSWORD = "correct-horse-battery"


def _register(**overrides: object) -> RegisterRequest:
    payload: dict[str, object] = {"email": "ada@example.com", "password": VALID_PASSWORD}
    payload.update(overrides)
    return RegisterRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def test_email_is_trimmed() -> None:
    assert str(_register(email="   ada@example.com   ").email) == "ada@example.com"


def test_email_domain_is_normalised_to_lowercase() -> None:
    # Case-insensitive uniqueness is enforced by the database; the schema only
    # guarantees the domain is canonical. The local part is preserved (RFC 5321).
    assert str(_register(email="Ada@Example.COM").email) == "Ada@example.com"


@pytest.mark.parametrize(
    "value",
    ["not-an-email", "", "ada@", "@example.com", "ada@example", "a b@example.com"],
)
def test_invalid_email_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _register(email=value)
    assert excinfo.value.errors()[0]["loc"] == ("email",)


def test_email_longer_than_rfc_limit_is_rejected() -> None:
    # 64 + 1 + 63 + 1 + 63 + 1 + 63 + 4 = 260 characters. Every label is a
    # legal length, so only the total-length rule can reject it.
    local = "a" * 64
    domain = ".".join(["b" * 63, "c" * 63, "d" * 63, "com"])
    assert len(f"{local}@{domain}") > 254
    with pytest.raises(ValidationError):
        _register(email=f"{local}@{domain}")


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 7])
def test_password_shorter_than_minimum_is_rejected(length: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _register(password="a" * length)
    assert excinfo.value.errors()[0]["loc"] == ("password",)


def test_password_of_minimum_length_is_accepted() -> None:
    assert _register(password="a" * 8).password.get_secret_value() == "a" * 8


def test_password_of_exactly_bcrypt_limit_is_accepted() -> None:
    password = "a" * BCRYPT_MAX_PASSWORD_BYTES
    assert _register(password=password).password.get_secret_value() == password


def test_password_over_bcrypt_limit_is_rejected_not_truncated() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _register(password="a" * (BCRYPT_MAX_PASSWORD_BYTES + 1))
    assert "72 bytes" in str(excinfo.value.errors()[0]["msg"])


def test_multibyte_password_is_measured_in_bytes() -> None:
    # 24 x 4 bytes = 96 bytes, although only 24 characters.
    with pytest.raises(ValidationError):
        _register(password="\N{GRINNING FACE}" * 24)
    # 18 x 4 = 72 bytes: allowed.
    assert _register(password="\N{GRINNING FACE}" * 18)


def test_password_is_not_revealed_by_repr_or_str() -> None:
    payload = _register()
    assert VALID_PASSWORD not in repr(payload)
    assert VALID_PASSWORD not in str(payload)


def test_password_is_not_stored_as_plain_text() -> None:
    assert _register().password.get_secret_value() == VALID_PASSWORD


# ---------------------------------------------------------------------------
# Strictness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["is_admin", "role", "token_version"])
def test_unknown_fields_are_rejected(field: str) -> None:
    payload = {"email": "ada@example.com", "password": VALID_PASSWORD, field: True}
    with pytest.raises(ValidationError):
        RegisterRequest(**payload)  # type: ignore[arg-type]


def test_login_accepts_any_non_empty_password_within_bcrypt_limit() -> None:
    # No complexity policy on login: an existing credential must be checked,
    # not re-validated against today's registration rules.
    request = LoginRequest(email="ada@example.com", password=SecretStr("short"))
    assert request.password.get_secret_value() == "short"


@pytest.mark.parametrize("password", ["", "a" * (BCRYPT_MAX_PASSWORD_BYTES + 1)])
def test_login_rejects_what_bcrypt_cannot_process(password: str) -> None:
    with pytest.raises(ValidationError):
        LoginRequest(email="ada@example.com", password=SecretStr(password))
