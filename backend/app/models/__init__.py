"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata`` — Alembic's
autogenerate depends on that, so keep the re-exports below in sync.
"""

from app.models.agent import Agent
from app.models.appointment import Appointment
from app.models.base import Base
from app.models.conversation import Conversation, Message
from app.models.followup import FollowUpTask
from app.models.lead import Lead
from app.models.listing import Listing

__all__ = [
    "Agent",
    "Appointment",
    "Base",
    "Conversation",
    "FollowUpTask",
    "Lead",
    "Listing",
    "Message",
]
