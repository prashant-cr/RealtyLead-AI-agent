import pytest

from app.agent.optout import is_opt_out, opt_out_confirmation


@pytest.mark.parametrize(
    "text",
    [
        "STOP",
        "stop",
        "  Stop  ",
        "unsubscribe",
        "Please remove me from your list",
        "not interested",
        "Not interested, thanks",
        "do not contact me again",
        "please stop messaging me",
        "band karo",
        "mujhe nahi chahiye",
        "बंद करो",
        "मुझे नहीं चाहिए",
        "bandh karo",
        "mane ras nathi",
        "બંધ કરો",
    ],
)
def test_opt_out_phrases_are_detected(text: str) -> None:
    assert is_opt_out(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Can we stop by the site on Sunday?",
        "I'm interested in the Bopal flat",
        "What's the stop for the metro nearby?",
        "yes please",
        "",
        "   ",
        "3 BHK under 80 lakhs",
        "I want to stop at the property on my way home",
    ],
)
def test_ordinary_messages_are_not_opt_outs(text: str) -> None:
    assert is_opt_out(text) is False


def test_confirmation_available_in_every_launch_language() -> None:
    for language in ("en", "hi", "gu"):
        assert opt_out_confirmation(language)
    # Unknown language falls back to English rather than blowing up.
    assert opt_out_confirmation("fr") == opt_out_confirmation("en")
