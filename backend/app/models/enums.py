"""Domain enums.

All of these are persisted as VARCHAR with a CHECK constraint (``native_enum=False``)
so migrations stay trivial when we add a value — no ALTER TYPE dances.
"""

from enum import StrEnum


class Language(StrEnum):
    ENGLISH = "en"
    HINDI = "hi"
    GUJARATI = "gu"


class PropertyType(StrEnum):
    FLAT = "flat"
    VILLA = "villa"
    PLOT = "plot"
    COMMERCIAL = "commercial"


class ListingStatus(StrEnum):
    AVAILABLE = "available"
    UNDER_OFFER = "under_offer"
    SOLD = "sold"
    WITHDRAWN = "withdrawn"


class LeadStatus(StrEnum):
    NEW = "new"
    ENGAGED = "engaged"
    QUALIFIED = "qualified"
    BOOKED = "booked"
    COLD = "cold"
    HANDED_OFF = "handed_off"
    OPTED_OUT = "opted_out"


class LeadTemperature(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class LeadPurpose(StrEnum):
    SELF_USE = "self_use"
    INVESTMENT = "investment"
    UNKNOWN = "unknown"


class ConsentStatus(StrEnum):
    """WhatsApp/TRAI: we may only message leads who opted in, until they opt out."""

    UNKNOWN = "unknown"
    OPTED_IN = "opted_in"
    OPTED_OUT = "opted_out"


class Channel(StrEnum):
    WHATSAPP = "whatsapp"
    SMS = "sms"
    EMAIL = "email"
    WEB = "web"
    CLI = "cli"  # local test harness (M2)


class MessageRole(StrEnum):
    LEAD = "lead"
    ASSISTANT = "assistant"
    HUMAN_AGENT = "human_agent"
    SYSTEM = "system"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    RECEIVED = "received"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    HUMAN_TAKEOVER = "human_takeover"
    CLOSED = "closed"


class AppointmentType(StrEnum):
    CALL = "call"
    SITE_VISIT = "site_visit"


class AppointmentStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class FollowUpStatus(StrEnum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    FAILED = "failed"
