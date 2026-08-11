import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services import oauth_state

SECRET = "state-signing-secret"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def test_state_round_trips_the_agent_id() -> None:
    agent_id = uuid.uuid4()

    state = oauth_state.issue(agent_id, SECRET, now=NOW)

    assert oauth_state.verify(state, SECRET, 600, now=NOW) == agent_id


def test_tampered_payload_is_rejected() -> None:
    """Otherwise anyone could bind their calendar to another agent's record."""
    state = oauth_state.issue(uuid.uuid4(), SECRET, now=NOW)
    payload, _, signature = state.partition(".")
    forged = oauth_state.issue(uuid.uuid4(), SECRET, now=NOW).partition(".")[0]

    with pytest.raises(oauth_state.InvalidStateError):
        oauth_state.verify(f"{forged}.{signature}", SECRET, 600, now=NOW)
    assert payload  # the original payload was well-formed


def test_wrong_secret_is_rejected() -> None:
    state = oauth_state.issue(uuid.uuid4(), "other-secret", now=NOW)

    with pytest.raises(oauth_state.InvalidStateError):
        oauth_state.verify(state, SECRET, 600, now=NOW)


def test_expired_state_is_rejected() -> None:
    state = oauth_state.issue(uuid.uuid4(), SECRET, now=NOW)

    with pytest.raises(oauth_state.InvalidStateError, match="expired"):
        oauth_state.verify(state, SECRET, 600, now=NOW + timedelta(seconds=601))


def test_state_within_ttl_is_accepted() -> None:
    agent_id = uuid.uuid4()
    state = oauth_state.issue(agent_id, SECRET, now=NOW)

    assert oauth_state.verify(state, SECRET, 600, now=NOW + timedelta(seconds=599)) == agent_id


@pytest.mark.parametrize("state", ["", "no-dot", "a.b", "....", "!!!.???"])
def test_malformed_states_are_rejected(state: str) -> None:
    with pytest.raises(oauth_state.InvalidStateError):
        oauth_state.verify(state, SECRET, 600, now=NOW)
