"""Profile model.

A profile is keyed by the opaque UUID owned by Auth. Profiles are created
lazily on first authenticated access; there is no user row in this database
and no cross-service foreign key (ADR-0002).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

DISPLAY_NAME_MAX_LENGTH = 100
PHONE_NUMBER_MAX_LENGTH = 32


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    display_name: Mapped[str | None] = mapped_column(
        String(DISPLAY_NAME_MAX_LENGTH), nullable=True
    )
    phone_number: Mapped[str | None] = mapped_column(
        String(PHONE_NUMBER_MAX_LENGTH), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


Index("ux_profiles_user_id", Profile.user_id, unique=True)


__all__ = [
    "DISPLAY_NAME_MAX_LENGTH",
    "PHONE_NUMBER_MAX_LENGTH",
    "Profile",
]
