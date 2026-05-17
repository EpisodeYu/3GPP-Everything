"""add message status column

Revision ID: a1b2c3d4e5f6
Revises: 9cf40059f3b1
Create Date: 2026-05-18 02:43:00+00:00

M4.7 Q9：assistant message 仅在 final event 后落 content；中断 → status='failed'/'cancelled'。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "9cf40059f3b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="ok",
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "status")
