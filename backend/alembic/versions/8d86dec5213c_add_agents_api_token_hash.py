"""add agents.api_token_hash

Revision ID: 8d86dec5213c
Revises: 6373a5f23d58
Create Date: 2026-08-11 16:20:19.840439

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d86dec5213c"
down_revision: str | None = "6373a5f23d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("api_token_hash", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_agents_api_token_hash"), "agents", ["api_token_hash"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_agents_api_token_hash"), table_name="agents")
    op.drop_column("agents", "api_token_hash")
