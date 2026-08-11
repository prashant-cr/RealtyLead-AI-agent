"""initial schema

Revision ID: a8f05a79263b
Revises:
Create Date: 2026-08-11 14:22:38.681573

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8f05a79263b"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("brokerage_name", sa.String(length=160), nullable=True),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("working_hours", sa.JSON(), nullable=False),
        sa.Column("quiet_hours_start", sa.Integer(), nullable=False),
        sa.Column("quiet_hours_end", sa.Integer(), nullable=False),
        sa.Column("tone_instructions", sa.Text(), nullable=True),
        sa.Column("escalation_budget_threshold", sa.Integer(), nullable=True),
        sa.Column("google_refresh_token", sa.Text(), nullable=True),
        sa.Column("google_calendar_id", sa.String(length=255), nullable=True),
        sa.Column("whatsapp_phone_number_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
    )
    op.create_index(op.f("ix_agents_email"), "agents", ["email"], unique=True)
    op.create_index(
        op.f("ix_agents_whatsapp_phone_number_id"),
        "agents",
        ["whatsapp_phone_number_id"],
        unique=False,
    )
    op.create_table(
        "listings",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "property_type",
            sa.Enum(
                "flat",
                "villa",
                "plot",
                "commercial",
                name="propertytype_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "available",
                "under_offer",
                "sold",
                "withdrawn",
                name="listingstatus_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("locality", sa.String(length=160), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("price", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("bhk", sa.Integer(), nullable=True),
        sa.Column("carpet_area_sqft", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rera_id", sa.String(length=64), nullable=True),
        sa.Column("media_urls", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_listings_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listings")),
    )
    op.create_index(op.f("ix_listings_agent_id"), "listings", ["agent_id"], unique=False)
    op.create_index("ix_listings_agent_status", "listings", ["agent_id", "status"], unique=False)
    op.create_index("ix_listings_city_type", "listings", ["city", "property_type"], unique=False)
    op.create_index(op.f("ix_listings_locality"), "listings", ["locality"], unique=False)
    op.create_table(
        "leads",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "language",
            sa.Enum("en", "hi", "gu", name="language_enum", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "engaged",
                "qualified",
                "booked",
                "cold",
                "handed_off",
                "opted_out",
                name="leadstatus_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column(
            "temperature",
            sa.Enum(
                "hot", "warm", "cold", name="leadtemperature_enum", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("score_reasons", sa.JSON(), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_min", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("budget_max", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("preferred_locations", sa.JSON(), nullable=False),
        sa.Column(
            "property_type",
            sa.Enum(
                "flat",
                "villa",
                "plot",
                "commercial",
                name="propertytype_enum",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("bhk", sa.Integer(), nullable=True),
        sa.Column("timeline_months", sa.Integer(), nullable=True),
        sa.Column("loan_preapproved", sa.Boolean(), nullable=True),
        sa.Column(
            "purpose",
            sa.Enum(
                "self_use",
                "investment",
                "unknown",
                name="leadpurpose_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("site_visit_willing", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("listing_id", sa.Uuid(), nullable=True),
        sa.Column(
            "consent_status",
            sa.Enum(
                "unknown",
                "opted_in",
                "opted_out",
                name="consentstatus_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_count", sa.Integer(), nullable=False),
        sa.Column("handed_off_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_reason", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_leads_agent_id_agents"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_leads_listing_id_listings"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
        sa.UniqueConstraint("agent_id", "phone", name="uq_leads_agent_id_phone"),
    )
    op.create_index(op.f("ix_leads_agent_id"), "leads", ["agent_id"], unique=False)
    op.create_index("ix_leads_agent_status", "leads", ["agent_id", "status"], unique=False)
    op.create_table(
        "appointments",
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=True),
        sa.Column(
            "appointment_type",
            sa.Enum(
                "call", "site_visit", name="appointmenttype_enum", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "confirmed",
                "cancelled",
                "completed",
                "no_show",
                name="appointmentstatus_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("google_event_id", sa.String(length=255), nullable=True),
        sa.Column("confirmation_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_appointments_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_appointments_lead_id_leads"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_appointments_listing_id_listings"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointments")),
    )
    op.create_index(op.f("ix_appointments_agent_id"), "appointments", ["agent_id"], unique=False)
    op.create_index(
        "ix_appointments_agent_starts_at", "appointments", ["agent_id", "starts_at"], unique=False
    )
    op.create_index(op.f("ix_appointments_lead_id"), "appointments", ["lead_id"], unique=False)
    op.create_table(
        "conversations",
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "whatsapp",
                "sms",
                "email",
                "web",
                "cli",
                name="channel_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "human_takeover",
                "closed",
                name="conversationstatus_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_conversations_lead_id_leads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ix_conversations_lead_channel", "conversations", ["lead_id", "channel"], unique=False
    )
    op.create_index(op.f("ix_conversations_lead_id"), "conversations", ["lead_id"], unique=False)
    op.create_table(
        "follow_up_tasks",
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "whatsapp",
                "sms",
                "email",
                "web",
                "cli",
                name="channel_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "scheduled",
                "sent",
                "skipped",
                "cancelled",
                "failed",
                name="followupstatus_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("template_name", sa.String(length=120), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_reason", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_follow_up_tasks_lead_id_leads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_follow_up_tasks")),
    )
    op.create_index(
        op.f("ix_follow_up_tasks_lead_id"), "follow_up_tasks", ["lead_id"], unique=False
    )
    op.create_index(
        "ix_follow_up_tasks_status_scheduled_for",
        "follow_up_tasks",
        ["status", "scheduled_for"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "lead",
                "assistant",
                "human_agent",
                "system",
                name="messagerole_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "direction",
            sa.Enum(
                "inbound", "outbound", name="messagedirection_enum", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column(
            "channel",
            sa.Enum(
                "whatsapp",
                "sms",
                "email",
                "web",
                "cli",
                name="channel_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "delivered",
                "read",
                "failed",
                "received",
                name="messagestatus_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("media_urls", sa.JSON(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("channel", "external_id", name="uq_messages_channel_external_id"),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_messages_conversation_id"), "messages", ["conversation_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_follow_up_tasks_status_scheduled_for", table_name="follow_up_tasks")
    op.drop_index(op.f("ix_follow_up_tasks_lead_id"), table_name="follow_up_tasks")
    op.drop_table("follow_up_tasks")
    op.drop_index(op.f("ix_conversations_lead_id"), table_name="conversations")
    op.drop_index("ix_conversations_lead_channel", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index(op.f("ix_appointments_lead_id"), table_name="appointments")
    op.drop_index("ix_appointments_agent_starts_at", table_name="appointments")
    op.drop_index(op.f("ix_appointments_agent_id"), table_name="appointments")
    op.drop_table("appointments")
    op.drop_index("ix_leads_agent_status", table_name="leads")
    op.drop_index(op.f("ix_leads_agent_id"), table_name="leads")
    op.drop_table("leads")
    op.drop_index(op.f("ix_listings_locality"), table_name="listings")
    op.drop_index("ix_listings_city_type", table_name="listings")
    op.drop_index("ix_listings_agent_status", table_name="listings")
    op.drop_index(op.f("ix_listings_agent_id"), table_name="listings")
    op.drop_table("listings")
    op.drop_index(op.f("ix_agents_whatsapp_phone_number_id"), table_name="agents")
    op.drop_index(op.f("ix_agents_email"), table_name="agents")
    op.drop_table("agents")
