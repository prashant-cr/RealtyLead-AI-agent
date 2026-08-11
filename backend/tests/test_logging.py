import logging

from app.core.logging import PIIMaskingFilter, mask_email, mask_name, mask_phone


def test_mask_phone_keeps_country_code_and_last_four() -> None:
    assert mask_phone("+919876543210") == "+91******3210"
    assert mask_phone("9876543210") == "98****3210"
    assert mask_phone(None) == "<none>"
    assert "1234" in mask_phone("+1 (415) 555-1234")


def test_mask_email_and_name() -> None:
    assert mask_email("priya.shah@example.com") == "p***h@example.com"
    assert mask_email("ab@example.com") == "a***@example.com"
    assert mask_email(None) == "<none>"
    assert mask_name("Priya Shah") == "P. S."


def test_filter_scrubs_pii_from_log_records() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="lead %s replied from +919876543210 (priya.shah@example.com)",
        args=("+919812345678",),
        exc_info=None,
    )

    assert PIIMaskingFilter().filter(record) is True
    rendered = record.getMessage()
    assert "9876543210" not in rendered
    assert "9812345678" not in rendered
    assert "priya.shah@example.com" not in rendered
    assert "example.com" in rendered
