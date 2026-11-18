"""User model.

Design notes
------------
* ``id`` is a UUID with a server-side default: public identifiers must not be
  enumerable (``/auth/me`` exposes the value to the client).
* Email uniqueness is case-insensitive. The database enforces it with a unique
  index on ``lower(email)``; the application never relies on a pre-check, so a
  concurrent duplicate becomes an ``IntegrityError`` rather than a race.
* No sessions/refresh-token table exists in Stage 1 (ADR-0003).
  ``token_version`` is reserved for future global revocation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# Case-insensitive uniqueness. Declared after the class so the mapped column is
# available; the migration creates the identical index explicitly.
Index("ux_users_email_lower", func.lower(User.email), unique=True)


__all__ = ["User"]
