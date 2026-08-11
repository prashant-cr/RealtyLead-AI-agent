from decimal import Decimal

from app.agent.scoring import (
    HOT_THRESHOLD,
    LeadTemperature,
    ScoringInput,
    active_listing_prices,
    score_lead,
    temperature_for,
)
from app.models.enums import LeadStatus, ListingStatus
from tests.factories import make_agent, make_listing

INVENTORY = [Decimal("6200000"), Decimal("8500000"), Decimal("21500000")]


def test_thresholds_match_the_spec() -> None:
    assert temperature_for(70) is LeadTemperature.HOT
    assert temperature_for(69) is LeadTemperature.WARM
    assert temperature_for(40) is LeadTemperature.WARM
    assert temperature_for(39) is LeadTemperature.COLD


def test_ready_buyer_scores_hot() -> None:
    result = score_lead(
        ScoringInput(
            budget_min=Decimal("6000000"),
            budget_max=Decimal("9000000"),
            timeline_months=2,
            loan_preapproved=True,
            site_visit_willing=True,
            inbound_message_count=6,
        ),
        INVENTORY,
    )

    assert result.score == 100
    assert result.temperature is LeadTemperature.HOT


def test_browser_scores_cold() -> None:
    result = score_lead(
        ScoringInput(timeline_months=24, loan_preapproved=False, inbound_message_count=1),
        INVENTORY,
    )

    assert result.score < 40
    assert result.temperature is LeadTemperature.COLD


def test_empty_profile_scores_zero_with_reasons() -> None:
    result = score_lead(ScoringInput(), INVENTORY)

    assert result.score == 0
    assert len(result.reasons) == 5
    assert all(r.detail for r in result.reasons)


def test_every_factor_explains_itself() -> None:
    result = score_lead(
        ScoringInput(
            budget_max=Decimal("9000000"),
            timeline_months=2,
            loan_preapproved=True,
            site_visit_willing=True,
            inbound_message_count=4,
        ),
        INVENTORY,
    )

    factors = {r.factor for r in result.reasons}
    assert factors == {"budget_match", "timeline", "financing", "responsiveness", "site_visit"}
    assert sum(r.points for r in result.reasons) == result.score
    assert all(isinstance(r.as_dict()["detail"], str) for r in result.reasons)


def test_budget_just_below_inventory_is_a_near_miss_not_a_zero() -> None:
    near = score_lead(ScoringInput(budget_max=Decimal("5800000")), INVENTORY)
    far = score_lead(ScoringInput(budget_max=Decimal("2000000")), INVENTORY)

    near_points = next(r.points for r in near.reasons if r.factor == "budget_match")
    far_points = next(r.points for r in far.reasons if r.factor == "budget_match")
    assert near_points == 15
    assert far_points == 0


def test_no_inventory_scores_budget_zero_and_says_why() -> None:
    result = score_lead(ScoringInput(budget_max=Decimal("9000000")), [])

    reason = next(r for r in result.reasons if r.factor == "budget_match")
    assert reason.points == 0
    assert "inventory" in reason.detail.lower()


def test_booked_lead_is_never_below_hot() -> None:
    result = score_lead(ScoringInput(status=LeadStatus.BOOKED), INVENTORY)

    assert result.score >= HOT_THRESHOLD
    assert result.temperature is LeadTemperature.HOT


def test_active_listing_prices_excludes_sold_and_inactive() -> None:
    agent = make_agent()
    listings = [
        make_listing(agent, price=Decimal("1"), status=ListingStatus.AVAILABLE, is_active=True),
        make_listing(agent, price=Decimal("2"), status=ListingStatus.SOLD, is_active=True),
        make_listing(agent, price=Decimal("3"), status=ListingStatus.AVAILABLE, is_active=False),
    ]

    assert active_listing_prices(listings) == [Decimal("1")]
