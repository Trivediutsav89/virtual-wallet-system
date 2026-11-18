"""baseline (empty)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-18

Stage 1 intentionally has no Wallet tables. This revision exists so the
service owns a real Alembic history from day one: Stage 2 adds
``0002_...`` on top of it, and the migration chain (and the
``alembic_version`` row) is already exercised by CI and Compose.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema objects yet."""


def downgrade() -> None:
    """Nothing to undo."""
