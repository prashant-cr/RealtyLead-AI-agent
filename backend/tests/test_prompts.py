import pytest

from app.agent import prompts


def test_latest_version_is_discovered() -> None:
    assert prompts.latest_version("qualification_system") >= 1


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
