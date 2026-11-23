"""Pydantic v2 request/response schemas for profiles."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.profile import DISPLAY_NAME_MAX_LENGTH, PHONE_NUMBER_MAX_LENGTH


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    display_name: str | None
    phone_number: str | None
    created_at: datetime
    updated_at: datetime


class ProfilePatchRequest(BaseModel):
    """Partial update.

    Omitted fields are left untouched; an explicit ``null`` clears the field.
    Unknown fields are rejected so clients cannot set ``user_id``, ids or
    timestamps.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    phone_number: str | None = None

    @field_validator("display_name", "phone_number", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty when provided")
        return stripped

    @field_validator("display_name")
    @classmethod
    def _limit_display_name(cls, value: str | None) -> str | None:
        if value is not None and len(value) > DISPLAY_NAME_MAX_LENGTH:
            raise ValueError(f"Display name must be at most {DISPLAY_NAME_MAX_LENGTH} characters")
        return value

    @field_validator("phone_number")
    @classmethod
    def _limit_phone_number(cls, value: str | None) -> str | None:
        if value is not None and len(value) > PHONE_NUMBER_MAX_LENGTH:
            raise ValueError(f"Phone number must be at most {PHONE_NUMBER_MAX_LENGTH} characters")
        return value


__all__ = ["ProfileOut", "ProfilePatchRequest"]
