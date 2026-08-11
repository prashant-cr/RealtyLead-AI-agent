"""add agent passwords and sessions

Revision ID: 5bc42c94fabf
Revises: 8d86dec5213c
Create Date: 2026-08-11 16:42:09.151162

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.models.types

revision: str = "5bc42c94fabf"
down_revision: str | None = "8d86dec5213c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", app.models.types.UtcDateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", app.models.types.UtcDateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", app.models.types.UtcDateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            app.models.types.UtcDateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.models.types.UtcDateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_agent_sessions_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_sessions")),
    )
    op.create_index(
        "ix_agent_sessions_agent_expires",
        "agent_sessions",
        ["agent_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_sessions_agent_id"), "agent_sessions", ["agent_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_sessions_token_hash"), "agent_sessions", ["token_hash"], unique=True
    )
    op.add_column("agents", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column(
        "agents",
        sa.Column("onboarded_at", app.models.types.UtcDateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "onboarded_at")
    op.drop_column("agents", "password_hash")
    op.drop_index(op.f("ix_agent_sessions_token_hash"), table_name="agent_sessions")
    op.drop_index(op.f("ix_agent_sessions_agent_id"), table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_agent_expires", table_name="agent_sessions")
    op.drop_table("agent_sessions")
