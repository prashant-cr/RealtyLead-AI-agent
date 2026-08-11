"""Lead scoring v1 — rule-based and explainable.

The dashboard has to show *why* a lead scored what it did, so every factor
returns a reason alongside its points. Weights sum to 100:

    budget match with inventory   30
    purchase timeline             25
    financing status              20
    responsiveness                15
    site-visit willingness        10

Thresholds: 70+ hot, 40-69 warm, below 40 cold. ML scoring is explicitly out of
scope for v1 (see docs/backlog.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models.enums import LeadStatus, LeadTemperature, ListingStatus

HOT_THRESHOLD = 70
WARM_THRESHOLD = 40

MAX_BUDGET_POINTS = 30
MAX_TIMELINE_POINTS = 25
MAX_FINANCING_POINTS = 20
MAX_RESPONSIVENESS_POINTS = 15
MAX_SITE_VISIT_POINTS = 10

# A listing within this fraction above the stated budget still counts as a near miss.
NEAR_MISS_TOLERANCE = Decimal("0.15")


@dataclass(frozen=True)
class ScoreReason:
    factor: str
    points: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"factor": self.factor, "points": self.points, "detail": self.detail}


@dataclass(frozen=True)
class LeadScore:
    score: int
    temperature: LeadTemperature
    reasons: list[ScoreReason] = field(default_factory=list)

    @property
    def reason_dicts(self) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self.reasons]


@dataclass(frozen=True)
class ScoringInput:
    """Everything scoring needs, decoupled from the ORM so it is trivial to test."""

    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    timeline_months: int | None = None
    loan_preapproved: bool | None = None
    site_visit_willing: bool | None = None
    inbound_message_count: int = 0
    status: LeadStatus = LeadStatus.NEW


def temperature_for(score: int) -> LeadTemperature:
    if score >= HOT_THRESHOLD:
        return LeadTemperature.HOT
    if score >= WARM_THRESHOLD:
        return LeadTemperature.WARM
    return LeadTemperature.COLD


def _budget_reason(data: ScoringInput, listing_prices: list[Decimal]) -> ScoreReason:
    if data.budget_max is None and data.budget_min is None:
        return ScoreReason("budget_match", 0, "Budget not shared yet")
    if not listing_prices:
        return ScoreReason("budget_match", 0, "No active inventory to match against")

    low = data.budget_min if data.budget_min is not None else Decimal(0)
    high = data.budget_max if data.budget_max is not None else Decimal(10) ** 12

    matches = [p for p in listing_prices if low <= p <= high]
    if matches:
        return ScoreReason(
            "budget_match",
            MAX_BUDGET_POINTS,
            f"Budget matches {len(matches)} of {len(listing_prices)} active listings",
        )

    stretch = high * (1 + NEAR_MISS_TOLERANCE)
    near = [p for p in listing_prices if high < p <= stretch]
    if near:
        return ScoreReason(
            "budget_match",
            MAX_BUDGET_POINTS // 2,
            f"{len(near)} listings within 15% above their stated budget",
        )
    return ScoreReason("budget_match", 0, "No active listing falls in their budget range")


def _timeline_reason(months: int | None) -> ScoreReason:
    if months is None:
        return ScoreReason("timeline", 0, "Purchase timeline not shared yet")
    if months <= 3:
        return ScoreReason("timeline", MAX_TIMELINE_POINTS, f"Buying within {months} months")
    if months <= 6:
        return ScoreReason("timeline", 15, f"Buying in about {months} months")
    if months <= 12:
        return ScoreReason("timeline", 8, f"Buying in about {months} months")
    return ScoreReason("timeline", 3, f"Timeline is {months}+ months out")


def _financing_reason(preapproved: bool | None) -> ScoreReason:
    if preapproved is None:
        return ScoreReason("financing", 0, "Loan status not shared yet")
    if preapproved:
        return ScoreReason("financing", MAX_FINANCING_POINTS, "Home loan pre-approved")
    return ScoreReason("financing", 3, "No loan pre-approval yet")


def _responsiveness_reason(count: int) -> ScoreReason:
    if count >= 6:
        return ScoreReason(
            "responsiveness", MAX_RESPONSIVENESS_POINTS, f"Engaged — {count} replies so far"
        )
    if count >= 3:
        return ScoreReason("responsiveness", 10, f"Replying consistently ({count} messages)")
    if count >= 1:
        return ScoreReason("responsiveness", 5, f"Early in the conversation ({count} messages)")
    return ScoreReason("responsiveness", 0, "Has not replied yet")


def _site_visit_reason(willing: bool | None) -> ScoreReason:
    if willing is None:
        return ScoreReason("site_visit", 0, "Not asked about a site visit yet")
    if willing:
        return ScoreReason("site_visit", MAX_SITE_VISIT_POINTS, "Willing to visit the property")
    return ScoreReason("site_visit", 0, "Not willing to visit yet")


def score_lead(data: ScoringInput, listing_prices: list[Decimal]) -> LeadScore:
    """Score a lead 0-100 and explain each factor."""
    reasons = [
        _budget_reason(data, listing_prices),
        _timeline_reason(data.timeline_months),
        _financing_reason(data.loan_preapproved),
        _responsiveness_reason(data.inbound_message_count),
        _site_visit_reason(data.site_visit_willing),
    ]
    total = sum(r.points for r in reasons)

    if data.status is LeadStatus.BOOKED:
        reasons.append(ScoreReason("booked", 0, "Appointment booked — treated as hot"))
        return LeadScore(max(total, HOT_THRESHOLD), LeadTemperature.HOT, reasons)

    return LeadScore(total, temperature_for(total), reasons)


def active_listing_prices(listings: list[Any]) -> list[Decimal]:
    """Prices of listings a lead could actually be sold today."""
    return [
        listing.price
        for listing in listings
        if listing.is_active and listing.status is ListingStatus.AVAILABLE
    ]
