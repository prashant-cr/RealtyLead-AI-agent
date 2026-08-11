"""Signed, expiring OAuth `state` values.

The `state` parameter carries which agent is connecting and protects against
CSRF. Both matter: an unsigned state would let anyone bind *their* Google
calendar to *someone else's* agent record just by crafting a callback URL.

Signed with HMAC rather than stored server-side so the flow stays stateless.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime


class InvalidStateError(ValueError):
    """The state was forged, tampered with, or has expired."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue(agent_id: uuid.UUID, secret: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    payload = json.dumps(
        {"agent_id": str(agent_id), "issued_at": int(now.timestamp())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def verify(state: str, secret: str, ttl_seconds: int, now: datetime | None = None) -> uuid.UUID:
    """Return the agent id carried by a valid state, or raise."""
    now = now or datetime.now(UTC)
    try:
        encoded_payload, encoded_signature = state.split(".", 1)
        payload = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise InvalidStateError("malformed state") from exc

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidStateError("state signature does not match")

    try:
        data = json.loads(payload)
        agent_id = uuid.UUID(data["agent_id"])
        issued_at = int(data["issued_at"])
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidStateError("state payload is not readable") from exc

    if now.timestamp() - issued_at > ttl_seconds:
        raise InvalidStateError("state has expired")
    return agent_id
