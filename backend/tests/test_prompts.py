import pytest

from app.agent import prompts


def test_latest_version_is_discovered() -> None:
    assert prompts.latest_version("qualification_system") >= 3


def test_missing_prompt_raises_rather_than_returning_empty() -> None:
    with pytest.raises(prompts.PromptNotFoundError):
        prompts.load("no_such_prompt")


def test_render_substitutes_every_placeholder() -> None:
    rendered = prompts.render(
        "qualification_system",
        agent_name="Rohan Mehta",
        brokerage_name="Sunrise Homes",
        agent_phone_masked="+91******0001",
        channel="whatsapp",
        language_instruction="Reply in English.",
        escalation_threshold="₹20,000,000",
        working_hours="mon 09:30-19:00",
        timezone="Asia/Kolkata",
        today="Wednesday 12 August 2026",
        lead_profile="- Nothing yet",
        listing_summary="This agent has 3 active listings.",
        tone_instructions="Keep it warm and brief.",
    )

    assert "$" not in rendered
    assert "Rohan Mehta" in rendered
    assert "Sunrise Homes" in rendered


def test_render_fails_loudly_on_a_missing_value() -> None:
    with pytest.raises(KeyError):
        prompts.render("qualification_system", agent_name="Rohan Mehta")


def test_prompt_encodes_the_non_negotiable_rules() -> None:
    text = prompts.load("qualification_system").lower()

    assert "ai assistant" in text
    assert "one question per message" in text
    assert "get_listing_details" in text
    assert "escalate_to_human" in text
    assert "negotiate" in text


def test_v2_carries_the_agents_own_tone() -> None:
    """M7 lets agents set their voice; v1 had no slot for it."""
    assert "$tone_instructions" in prompts.load("qualification_system", version=2)
    assert "$tone_instructions" not in prompts.load("qualification_system", version=1)


def test_older_prompt_versions_stay_loadable() -> None:
    """Versioning exists so a regression can be rolled back, not just archived."""
    assert prompts.load("qualification_system", version=1)


def test_v3_pins_offered_slots_and_suppresses_needless_corrections() -> None:
    """Both were seen in live conversations against the real model. v2 offered a
    different set of times on every turn, which reads as though the slots are not
    real. An earlier draft of v3 then over-corrected and had the model opening
    turns with "I should correct my last message" when nothing had been wrong."""
    text = prompts.load("qualification_system", version=3).lower()

    assert "keep offering those same" in text
    assert "don't narrate corrections" in text
