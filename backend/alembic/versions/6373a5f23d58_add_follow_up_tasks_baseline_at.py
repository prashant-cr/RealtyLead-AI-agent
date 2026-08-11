"""add follow_up_tasks.baseline_at

Revision ID: 6373a5f23d58
Revises: a8f05a79263b
Create Date: 2026-08-11 16:13:20.351143

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.models.types

revision: str = "6373a5f23d58"
down_revision: str | None = "a8f05a79263b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "follow_up_tasks",
        sa.Column("baseline_at", app.models.types.UtcDateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("follow_up_tasks", "baseline_at")
