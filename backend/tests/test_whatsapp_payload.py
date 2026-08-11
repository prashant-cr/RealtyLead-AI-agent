import json

from app.channels.whatsapp import normalise_phone, to_wa_id
from app.channels.whatsapp_payload import parse_webhook, sign, verify_signature
from app.models.enums import Channel, MessageStatus

SECRET = "shhh-app-secret"


def envelope(value: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": value}]}],
    }


def message_value(message: dict, *, contacts: list | None = None) -> dict:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001111", "phone_number_id": "PNID123"},
        "contacts": contacts
        if contacts is not None
        else [{"profile": {"name": "Priya Shah"}, "wa_id": "919876543210"}],
        "messages": [message],
    }


TEXT_MESSAGE = {
    "from": "919876543210",
    "id": "wamid.TEXT1",
    "timestamp": "1786500000",
    "type": "text",
    "text": {"body": "Is the Bopal flat available?"},
}


# ------------------------------------------------------------------- signatures


def test_valid_signature_is_accepted() -> None:
    body = json.dumps(envelope(message_value(TEXT_MESSAGE))).encode()

    assert verify_signature(body, sign(body, SECRET), SECRET) is True


def test_tampered_body_is_rejected() -> None:
    body = b'{"entry": []}'
    header = sign(body, SECRET)

    assert verify_signature(b'{"entry": [{"evil": true}]}', header, SECRET) is False


def test_wrong_secret_is_rejected() -> None:
    body = b'{"entry": []}'

    assert verify_signature(body, sign(body, "other-secret"), SECRET) is False


def test_missing_or_malformed_header_is_rejected() -> None:
    body = b'{"entry": []}'

    assert verify_signature(body, None, SECRET) is False
    assert verify_signature(body, "", SECRET) is False
    assert verify_signature(body, "sha1=abc", SECRET) is False
    assert verify_signature(body, sign(body, SECRET).removeprefix("sha256="), SECRET) is False


# ---------------------------------------------------------------------- phones


def test_phone_normalisation_round_trips() -> None:
    assert normalise_phone("919876543210") == "+919876543210"
    assert normalise_phone("+919876543210") == "+919876543210"
    assert to_wa_id("+919876543210") == "919876543210"


# ---------------------------------------------------------------------- parsing


def test_text_message_is_parsed() -> None:
    parsed = parse_webhook(envelope(message_value(TEXT_MESSAGE)))

    assert len(parsed.messages) == 1
    message = parsed.messages[0]
    assert message.channel is Channel.WHATSAPP
    assert message.sender == "+919876543210"
    assert message.text == "Is the Bopal flat available?"
    assert message.external_id == "wamid.TEXT1"
    assert message.recipient == "PNID123"
    assert message.raw["profile_name"] == "Priya Shah"
    assert message.received_at is not None


def test_image_without_caption_becomes_a_readable_placeholder() -> None:
    parsed = parse_webhook(
        envelope(
            message_value(
                {
                    "from": "919876543210",
                    "id": "wamid.IMG",
                    "type": "image",
                    "image": {"id": "MEDIA1", "mime_type": "image/jpeg"},
                }
            )
        )
    )

    message = parsed.messages[0]
    assert message.text == "[the lead sent an image]"
    assert message.media_urls == ["MEDIA1"]


def test_image_caption_is_preserved_alongside_the_placeholder() -> None:
    parsed = parse_webhook(
        envelope(
            message_value(
                {
                    "from": "919876543210",
                    "id": "wamid.IMG2",
                    "type": "image",
                    "image": {"id": "MEDIA2", "caption": "Is this the same layout?"},
                }
            )
        )
    )

    assert parsed.messages[0].text == "[the lead sent an image] Is this the same layout?"


def test_button_reply_uses_the_chosen_title() -> None:
    parsed = parse_webhook(
        envelope(
            message_value(
                {
                    "from": "919876543210",
                    "id": "wamid.BTN",
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": "yes", "title": "Yes, book a visit"},
                    },
                }
            )
        )
    )

    assert parsed.messages[0].text == "Yes, book a visit"


def test_location_share_is_described() -> None:
    parsed = parse_webhook(
        envelope(
            message_value(
                {
                    "from": "919876543210",
                    "id": "wamid.LOC",
                    "type": "location",
                    "location": {"latitude": 23.0, "longitude": 72.5, "name": "Bopal Circle"},
                }
            )
        )
    )

    assert "Bopal Circle" in parsed.messages[0].text


def test_unknown_message_type_does_not_crash() -> None:
    parsed = parse_webhook(
        envelope(message_value({"from": "919876543210", "id": "wamid.X", "type": "reaction"}))
    )

    assert len(parsed.messages) == 1
    assert parsed.messages[0].text.startswith("[the lead sent something")


def test_malformed_message_is_skipped_not_raised() -> None:
    parsed = parse_webhook(
        envelope(
            {
                "metadata": {"phone_number_id": "PNID123"},
                "messages": [{"id": "wamid.NOFROM", "type": "text", "text": {"body": "hi"}}],
            }
        )
    )

    assert parsed.messages == []


def test_several_messages_in_one_delivery_are_all_parsed() -> None:
    value = message_value(TEXT_MESSAGE)
    value["messages"].append({**TEXT_MESSAGE, "id": "wamid.TEXT2", "text": {"body": "hello?"}})

    parsed = parse_webhook(envelope(value))

    assert [m.external_id for m in parsed.messages] == ["wamid.TEXT1", "wamid.TEXT2"]


def test_empty_and_junk_payloads_parse_to_nothing() -> None:
    assert not parse_webhook({})
    assert not parse_webhook({"entry": []})
    assert not parse_webhook({"entry": [{"changes": [{"value": {}}]}]})


# --------------------------------------------------------------------- statuses


def test_status_updates_are_mapped() -> None:
    parsed = parse_webhook(
        envelope(
            {
                "metadata": {"phone_number_id": "PNID123"},
                "statuses": [
                    {"id": "wamid.OUT1", "status": "delivered", "timestamp": "1786500001"},
                    {"id": "wamid.OUT2", "status": "read", "timestamp": "1786500002"},
                ],
            }
        )
    )

    assert [s.status for s in parsed.statuses] == [
        MessageStatus.DELIVERED,
        MessageStatus.READ,
    ]
    assert parsed.statuses[0].external_id == "wamid.OUT1"


def test_failed_status_carries_the_error() -> None:
    parsed = parse_webhook(
        envelope(
            {
                "metadata": {"phone_number_id": "PNID123"},
                "statuses": [
                    {
                        "id": "wamid.OUT3",
                        "status": "failed",
                        "errors": [{"code": 131047, "title": "Re-engagement message"}],
                    }
                ],
            }
        )
    )

    assert parsed.statuses[0].status is MessageStatus.FAILED
    assert parsed.statuses[0].error == "Re-engagement message"


def test_unknown_status_is_ignored() -> None:
    parsed = parse_webhook(
        envelope(
            {
                "metadata": {"phone_number_id": "PNID123"},
                "statuses": [{"id": "wamid.OUT4", "status": "warp_speed"}],
            }
        )
    )

    assert parsed.statuses == []
