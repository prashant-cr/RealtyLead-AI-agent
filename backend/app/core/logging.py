"""Logging setup with PII masking.

Compliance rule: phone numbers, emails and lead names must never reach the logs
in clear text. Anything formatted through the stdlib logger is scrubbed by
`PIIMaskingFilter`; use `mask_phone`/`mask_email` explicitly when you build a
log message so the output stays readable.
"""

import logging
import re
from typing import Any

_PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\s\-()]{7,17}\d)(?!\w)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def mask_phone(phone: str | None) -> str:
    """+919876543210 -> +91******3210"""
    if not phone:
        return "<none>"
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return "*" * len(digits)
    country = digits[:2] if len(digits) > 8 else ""
    prefix = ("+" if phone.strip().startswith("+") else "") + country
    stars = "*" * max(len(digits) - len(country) - 4, 1)
    return f"{prefix}{stars}{digits[-4:]}"


def mask_email(email: str | None) -> str:
    """jane.doe@example.com -> j***e@example.com"""
    if not email or "@" not in email:
        return "<none>"
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def mask_name(name: str | None) -> str:
    """Priya Sharma -> P. S."""
    if not name:
        return "<none>"
    return " ".join(f"{part[0]}." for part in name.split() if part)


def _scrub(text: str) -> str:
    text = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), text)


class PIIMaskingFilter(logging.Filter):
    """Last line of defence: scrub phones/emails from any record that slips through."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _scrub(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub(a) if isinstance(a, str) else a for a in record.args)
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s"))
    handler.addFilter(PIIMaskingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).addFilter(PIIMaskingFilter())


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter[logging.Logger]:
    return logging.LoggerAdapter(logging.getLogger(name), context)
