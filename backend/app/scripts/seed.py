"""Load a small demo dataset so the API and (later) the dashboard have something to show.

Idempotent: re-running updates nothing and inserts nothing if the demo agent exists.

    make seed
"""

import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.core.db import dispose_engine, get_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.models import Agent, Lead, Listing
from app.models.enums import Language, LeadStatus, LeadTemperature, PropertyType

log = get_logger(__name__)

DEMO_AGENT_EMAIL = "demo.agent@sunrisehomes.example"


async def seed() -> None:
    async with get_sessionmaker()() as session:
        existing = await session.scalar(select(Agent).where(Agent.email == DEMO_AGENT_EMAIL))
        if existing is not None:
            log.info("demo data already present, nothing to do")
            return

        agent = Agent(
            name="Rohan Mehta",
            email=DEMO_AGENT_EMAIL,
            phone="+919876500001",
            brokerage_name="Sunrise Homes",
            languages=[Language.ENGLISH.value, Language.HINDI.value, Language.GUJARATI.value],
            timezone="Asia/Kolkata",
            escalation_budget_threshold=20_000_000,
            tone_instructions="Warm, brief, professional. Use the customer's name once.",
        )
        session.add(agent)
        await session.flush()

        session.add_all(
            [
                Listing(
                    agent_id=agent.id,
                    title="3 BHK in Bopal, Ahmedabad",
                    property_type=PropertyType.FLAT,
                    city="Ahmedabad",
                    locality="Bopal",
                    state="Gujarat",
                    price=Decimal("8500000.00"),
                    bhk=3,
                    carpet_area_sqft=1450,
                    rera_id="PR/GJ/AHMEDABAD/AUDA/RAA12345/010124",
                    description="East-facing, 8th floor, covered parking, gated society.",
                ),
                Listing(
                    agent_id=agent.id,
                    title="2 BHK near SG Highway",
                    property_type=PropertyType.FLAT,
                    city="Ahmedabad",
                    locality="SG Highway",
                    state="Gujarat",
                    price=Decimal("6200000.00"),
                    bhk=2,
                    carpet_area_sqft=1080,
                ),
                Listing(
                    agent_id=agent.id,
                    title="Villa in Shela",
                    property_type=PropertyType.VILLA,
                    city="Ahmedabad",
                    locality="Shela",
                    state="Gujarat",
                    price=Decimal("21500000.00"),
                    bhk=4,
                    carpet_area_sqft=3100,
                ),
            ]
        )

        session.add_all(
            [
                Lead(
                    agent_id=agent.id,
                    name="Priya Shah",
                    phone="+919876543210",
                    source="portal",
                    language=Language.ENGLISH,
                    status=LeadStatus.NEW,
                ),
                Lead(
                    agent_id=agent.id,
                    name="Amit Patel",
                    phone="+919812345678",
                    source="website_form",
                    language=Language.GUJARATI,
                    status=LeadStatus.ENGAGED,
                    budget_min=Decimal("6000000.00"),
                    budget_max=Decimal("9000000.00"),
                    preferred_locations=["Bopal", "Shela"],
                    property_type=PropertyType.FLAT,
                    bhk=3,
                    timeline_months=2,
                    loan_preapproved=True,
                    score=78,
                    temperature=LeadTemperature.HOT,
                    score_reasons=[
                        {"factor": "timeline", "points": 25, "detail": "Buying within 2 months"},
                        {"factor": "budget_match", "points": 30, "detail": "Matches 2 listings"},
                        {"factor": "financing", "points": 23, "detail": "Loan pre-approved"},
                    ],
                ),
            ]
        )

        await session.commit()
        log.info("seeded demo agent with 3 listings and 2 leads")


def main() -> None:
    configure_logging()
    asyncio.run(_run())


async def _run() -> None:
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    main()
