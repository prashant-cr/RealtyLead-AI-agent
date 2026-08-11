"""Settings, CSV listing import, and the onboarding checklist."""

from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import generate_token, hash_token
from app.core.config import Settings
from app.models import Agent, Listing
from app.models.enums import ListingStatus, PropertyType
from app.services.listing_import import parse_csv, parse_price, parse_property_type
from tests.factories import make_agent

ClientFactory = Callable[..., Awaitable[AsyncClient]]


def app_settings() -> Settings:
    return Settings(whatsapp_access_token="tok")


@pytest.fixture
async def api(client_factory: ClientFactory) -> AsyncClient:
    return await client_factory(app_settings())


async def make_authed(session: AsyncSession, **overrides: object) -> tuple[Agent, dict[str, str]]:
    token = generate_token()
    agent = make_agent(api_token_hash=hash_token(token), **overrides)
    session.add(agent)
    await session.flush()
    return agent, {"Authorization": f"Bearer {token}"}


def csv_bytes(text: str) -> bytes:
    return text.encode()


HEADER = "title,property_type,city,locality,price,bhk,carpet_area_sqft\n"


# ------------------------------------------------------------- price parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8500000", Decimal("8500000.00")),
        ("85,00,000", Decimal("8500000.00")),
        ("85 lakh", Decimal("8500000.00")),
        ("85L", Decimal("8500000.00")),
        ("85 Lakhs", Decimal("8500000.00")),
        ("1.2 cr", Decimal("12000000.00")),
        ("2.15 Crore", Decimal("21500000.00")),
        ("₹62,00,000", Decimal("6200000.00")),
    ],
)
def test_indian_price_formats_are_understood(raw: str, expected: Decimal) -> None:
    """Agents type prices the way they say them, not the way a database wants them."""
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw", ["", "expensive", "-5", "0"])
def test_bad_prices_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_price(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("flat", PropertyType.FLAT),
        ("Apartment", PropertyType.FLAT),
        ("VILLA", PropertyType.VILLA),
        ("bungalow", PropertyType.VILLA),
        ("plot", PropertyType.PLOT),
        ("Office", PropertyType.COMMERCIAL),
        ("", PropertyType.FLAT),
    ],
)
def test_property_type_aliases(raw: str, expected: PropertyType) -> None:
    assert parse_property_type(raw) == expected


# --------------------------------------------------------------- CSV parsing


def test_a_clean_file_parses() -> None:
    result = parse_csv(
        csv_bytes(HEADER + "3 BHK in Bopal,flat,Ahmedabad,Bopal,85 lakh,3,1450\n"), "agent-1"
    )

    assert result.ok
    assert len(result.listings) == 1
    listing = result.listings[0]
    assert listing.title == "3 BHK in Bopal"
    assert listing.price == Decimal("8500000.00")
    assert listing.bhk == 3
    assert listing.status is ListingStatus.AVAILABLE


def test_header_aliases_are_accepted() -> None:
    """Portal exports use their own column names."""
    result = parse_csv(
        csv_bytes(
            "Property Name,Type,City,Area,Cost,Bedrooms\nVilla,villa,Ahmedabad,Shela,2 cr,4\n"
        ),
        "agent-1",
    )

    assert result.ok, result.errors
    assert result.listings[0].locality == "Shela"
    assert result.listings[0].bhk == 4


def test_units_inside_cells_are_tolerated() -> None:
    result = parse_csv(
        csv_bytes(HEADER + 'Flat,flat,Ahmedabad,Bopal,85 lakh,3 BHK,"1,450 sqft"\n'), "agent-1"
    )

    assert result.ok, result.errors
    assert result.listings[0].bhk == 3
    assert result.listings[0].carpet_area_sqft == 1450


def test_missing_required_columns_are_reported_clearly() -> None:
    result = parse_csv(csv_bytes("title,bhk\nFlat,3\n"), "agent-1")

    assert not result.ok
    assert "city" in result.errors[0].message
    assert "price" in result.errors[0].message


def test_bad_rows_are_reported_with_line_numbers() -> None:
    content = HEADER + (
        "Good,flat,Ahmedabad,Bopal,85 lakh,3,1450\n"
        "Bad,flat,Ahmedabad,Bopal,about a crore maybe,3,1450\n"
    )

    result = parse_csv(csv_bytes(content), "agent-1")

    assert not result.ok
    assert result.errors[0].line == 3
    assert "price" in result.errors[0].message.lower()


def test_blank_rows_are_skipped_not_failed() -> None:
    result = parse_csv(
        csv_bytes(HEADER + "Flat,flat,Ahmedabad,Bopal,85 lakh,3,1450\n\n,,,,,,\n"), "agent-1"
    )

    assert result.ok
    assert len(result.listings) == 1
    assert result.skipped_blank == 2


def test_a_file_with_only_a_header_is_an_error() -> None:
    result = parse_csv(csv_bytes(HEADER), "agent-1")

    assert not result.ok


def test_utf8_bom_from_excel_is_handled() -> None:
    content = ("﻿" + HEADER + "Flat,flat,Ahmedabad,Bopal,85 lakh,3,1450\n").encode("utf-8")

    result = parse_csv(content, "agent-1")

    assert result.ok, result.errors


# ------------------------------------------------------------ import endpoint


async def test_import_adds_listings(api: AsyncClient, session: AsyncSession) -> None:
    agent, headers = await make_authed(session)

    response = await api.post(
        "/api/listings/import",
        headers=headers,
        files={"file": ("listings.csv", HEADER + "Flat,flat,Ahmedabad,Bopal,85 lakh,3,1450\n")},
    )

    assert response.status_code == 200
    assert response.json()["imported"] == 1
    listings = (await session.execute(select(Listing))).scalars().all()
    assert len(listings) == 1
    assert listings[0].agent_id == agent.id


async def test_a_file_with_any_bad_row_imports_nothing(
    api: AsyncClient, session: AsyncSession
) -> None:
    """Half a catalogue would make the assistant quote from incomplete inventory."""
    _, headers = await make_authed(session)
    content = HEADER + (
        "Good,flat,Ahmedabad,Bopal,85 lakh,3,1450\nBad,flat,Ahmedabad,Bopal,nonsense,3,1450\n"
    )

    response = await api.post(
        "/api/listings/import", headers=headers, files={"file": ("listings.csv", content)}
    )

    assert response.json()["imported"] == 0
    assert response.json()["errors"][0]["line"] == 3
    assert (await session.execute(select(Listing))).scalars().all() == []


async def test_replace_swaps_the_whole_inventory(api: AsyncClient, session: AsyncSession) -> None:
    agent, headers = await make_authed(session)
    session.add(
        Listing(
            agent_id=agent.id,
            title="Old",
            city="Ahmedabad",
            price=Decimal("1"),
            property_type=PropertyType.FLAT,
        )
    )
    await session.flush()

    response = await api.post(
        "/api/listings/import?replace=true",
        headers=headers,
        files={"file": ("listings.csv", HEADER + "New,flat,Ahmedabad,Bopal,85 lakh,3,1450\n")},
    )

    assert response.json()["replaced"] == 1
    titles = [listing.title for listing in (await session.execute(select(Listing))).scalars().all()]
    assert titles == ["New"]


async def test_import_requires_authentication(api: AsyncClient) -> None:
    response = await api.post("/api/listings/import", files={"file": ("listings.csv", HEADER)})

    assert response.status_code == 401


async def test_the_sample_csv_imports_cleanly(api: AsyncClient, session: AsyncSession) -> None:
    """The file we hand agents must actually work when they upload it back."""
    _, headers = await make_authed(session)
    sample = (await api.get("/api/listings/sample.csv")).text

    response = await api.post(
        "/api/listings/import", headers=headers, files={"file": ("sample.csv", sample)}
    )

    assert response.status_code == 200
    assert response.json()["imported"] == 3
    assert response.json()["errors"] == []


# ------------------------------------------------------------------ settings


async def test_settings_round_trip(api: AsyncClient, session: AsyncSession) -> None:
    _, headers = await make_authed(session)

    response = await api.patch(
        "/api/settings",
        headers=headers,
        json={
            "tone_instructions": "Warm, brief, use their first name once.",
            "working_hours": {"mon": ["10:00", "18:00"], "sun": []},
            "quiet_hours_start": 22,
            "languages": ["en", "hi"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tone_instructions"].startswith("Warm")
    assert body["working_hours"]["mon"] == ["10:00", "18:00"]
    assert body["quiet_hours_start"] == 22
    assert body["languages"] == ["en", "hi"]


async def test_partial_updates_leave_other_settings_alone(
    api: AsyncClient, session: AsyncSession
) -> None:
    agent, headers = await make_authed(session)
    original_hours = dict(agent.working_hours)

    await api.patch("/api/settings", headers=headers, json={"tone_instructions": "Be brief."})

    await session.refresh(agent)
    assert agent.working_hours == original_hours
    assert agent.tone_instructions == "Be brief."


async def test_a_typo_in_the_timezone_is_refused(api: AsyncClient, session: AsyncSession) -> None:
    """Silently falling back to Asia/Kolkata would misfire every quiet-hours check."""
    _, headers = await make_authed(session)

    response = await api.patch("/api/settings", headers=headers, json={"timezone": "Asia/Kolkatta"})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "hours",
    [
        {"funday": ["09:00", "17:00"]},
        {"mon": ["9am", "5pm"]},
        {"mon": ["18:00", "09:00"]},
        {"mon": ["09:00"]},
    ],
)
async def test_invalid_working_hours_are_refused(
    api: AsyncClient, session: AsyncSession, hours: dict[str, list[str]]
) -> None:
    _, headers = await make_authed(session)

    response = await api.patch("/api/settings", headers=headers, json={"working_hours": hours})

    assert response.status_code == 422


async def test_a_closed_day_is_allowed(api: AsyncClient, session: AsyncSession) -> None:
    _, headers = await make_authed(session)

    response = await api.patch(
        "/api/settings", headers=headers, json={"working_hours": {"sun": []}}
    )

    assert response.status_code == 200


# ---------------------------------------------------------------- checklist


async def test_checklist_reflects_what_is_missing(api: AsyncClient, session: AsyncSession) -> None:
    _, headers = await make_authed(session, whatsapp_phone_number_id=None)

    body = (await api.get("/api/onboarding", headers=headers)).json()

    steps = {step["key"]: step["done"] for step in body["steps"]}
    assert steps["account"] is True
    assert steps["hours"] is True  # defaults are set at signup
    assert steps["listings"] is False
    assert steps["whatsapp"] is False
    assert body["complete"] is False


async def test_checklist_completes_once_the_essentials_are_done(
    api: AsyncClient, session: AsyncSession
) -> None:
    agent, headers = await make_authed(session, whatsapp_phone_number_id="PNID1")
    await api.post(
        "/api/listings/import",
        headers=headers,
        files={"file": ("listings.csv", HEADER + "Flat,flat,Ahmedabad,Bopal,85 lakh,3,1450\n")},
    )

    body = (await api.get("/api/onboarding", headers=headers)).json()

    assert body["complete"] is True
    await session.refresh(agent)
    assert agent.onboarded_at is not None


async def test_optional_steps_do_not_block_completion(
    api: AsyncClient, session: AsyncSession
) -> None:
    """An agent with no calendar and no custom tone can still take leads today."""
    _, headers = await make_authed(session, whatsapp_phone_number_id="PNID1")
    await api.post(
        "/api/listings/import",
        headers=headers,
        files={"file": ("listings.csv", HEADER + "Flat,flat,Ahmedabad,Bopal,85 lakh,3,1450\n")},
    )

    body = (await api.get("/api/onboarding", headers=headers)).json()
    optional = {s["key"]: s["done"] for s in body["steps"] if s["key"] in ("calendar", "tone")}

    assert body["complete"] is True
    assert optional == {"calendar": False, "tone": False}


async def test_listings_are_scoped_to_the_agent(api: AsyncClient, session: AsyncSession) -> None:
    agent_a, headers_a = await make_authed(session)
    agent_b, _ = await make_authed(session, email="other@example.com", phone="+919876500099")
    session.add(
        Listing(
            agent_id=agent_b.id,
            title="Theirs",
            city="Mumbai",
            price=Decimal("1"),
            property_type=PropertyType.FLAT,
        )
    )
    await session.flush()

    body = (await api.get("/api/listings", headers=headers_a)).json()

    assert body == []
