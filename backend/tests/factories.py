"""Small hand-rolled factories — no extra dependency needed for M1."""

from decimal import Decimal
from typing import Any

from app.models import Agent, Lead, Listing
from app.models.enums import Language, PropertyType


def make_agent(**overrides: Any) -> Agent:
    defaults: dict[str, Any] = {
        "name": "Rohan Mehta",
        "email": "rohan@sunrisehomes.example",
        "phone": "+919876500001",
        "brokerage_name": "Sunrise Homes",
        "languages": [Language.ENGLISH.value, Language.HINDI.value],
        "timezone": "Asia/Kolkata",
    }
    return Agent(**{**defaults, **overrides})


def make_listing(agent: Agent, **overrides: Any) -> Listing:
    defaults: dict[str, Any] = {
        "agent_id": agent.id,
        "title": "3 BHK in Bopal",
        "property_type": PropertyType.FLAT,
        "city": "Ahmedabad",
        "locality": "Bopal",
        "price": Decimal("8500000.00"),
        "bhk": 3,
        "carpet_area_sqft": 1450,
    }
    return Listing(**{**defaults, **overrides})


def make_lead(agent: Agent, **overrides: Any) -> Lead:
    defaults: dict[str, Any] = {
        "agent_id": agent.id,
        "name": "Priya Shah",
        "phone": "+919876543210",
        "source": "portal",
    }
    return Lead(**{**defaults, **overrides})
