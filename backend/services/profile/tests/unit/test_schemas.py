"""Validation rules for profile patches."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.profile import DISPLAY_NAME_MAX_LENGTH, PHONE_NUMBER_MAX_LENGTH
from app.schemas.profile import ProfilePatchRequest


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProfilePatchRequest(user_id="11111111-1111-4111-8111-111111111111")  # type: ignore[call-arg]


def test_patch_is_partial_and_null_clears_a_field() -> None:
    patch = ProfilePatchRequest(display_name=None)
    assert patch.model_dump(exclude_unset=True) == {"display_name": None}


def test_text_is_stripped() -> None:
    patch = ProfilePatchRequest(display_name="  Ada  ", phone_number="  +91 99999 ")
    assert patch.display_name == "Ada"
    assert patch.phone_number == "+91 99999"


def test_empty_string_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ProfilePatchRequest(display_name="   ")


def test_display_name_length_is_limited() -> None:
    with pytest.raises(ValidationError):
        ProfilePatchRequest(display_name="x" * (DISPLAY_NAME_MAX_LENGTH + 1))


def test_phone_number_length_is_limited() -> None:
    with pytest.raises(ValidationError):
        ProfilePatchRequest(phone_number="x" * (PHONE_NUMBER_MAX_LENGTH + 1))


def test_boundary_lengths_are_accepted() -> None:
    patch = ProfilePatchRequest(
        display_name="x" * DISPLAY_NAME_MAX_LENGTH,
        phone_number="x" * PHONE_NUMBER_MAX_LENGTH,
    )
    assert len(patch.display_name or "") == DISPLAY_NAME_MAX_LENGTH
    assert len(patch.phone_number or "") == PHONE_NUMBER_MAX_LENGTH
