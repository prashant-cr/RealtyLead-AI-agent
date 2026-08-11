"""Application settings, loaded from environment / .env."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- app ---
    app_name: str = "RealtyLead AI Agent"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"

    # --- datastores ---
    database_url: str = "postgresql+asyncpg://realtylead:realtylead@localhost:5432/realtylead"
    redis_url: str = "redis://localhost:6379/0"
    db_echo: bool = False

    # --- Anthropic (M2) ---
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    # WhatsApp replies are short and the conversation is simple; "low" keeps latency
    # down. Raise to "medium"/"high" if qualification quality suffers.
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    anthropic_max_tokens: int = Field(default=1024, ge=64)
    # Safety valve on the tool loop — one turn should never need more than a few rounds.
    max_tool_iterations: int = Field(default=6, ge=1)

    # --- conversation defaults (per-agent values override these) ---
    default_timezone: str = "Asia/Kolkata"
    quiet_hours_start: int = Field(default=21, ge=0, le=23)  # 9pm local
    quiet_hours_end: int = Field(default=9, ge=0, le=23)  # 9am local
    max_follow_ups: int = Field(default=6, ge=0)
    escalation_budget_threshold: int = Field(default=20_000_000, ge=0)  # INR

    # --- external timeouts / retries (every outbound call uses these) ---
    http_timeout_seconds: float = 10.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 0.5

    # --- Google Calendar (M4) ---
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_oauth_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_oauth_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_oauth_token_url: str = "https://oauth2.googleapis.com/token"
    google_api_base: str = "https://www.googleapis.com/calendar/v3"
    # calendar.events is enough to read free/busy and write events — we never need
    # to list or modify the agent's calendars themselves.
    google_oauth_scope: str = "https://www.googleapis.com/auth/calendar.events"
    # Signs the OAuth `state` parameter (CSRF protection + agent identity).
    oauth_state_secret: str | None = None
    oauth_state_ttl_seconds: int = 600

    # --- WhatsApp Business Cloud API (M3) ---
    whatsapp_access_token: str | None = None
    # Echoed back to Meta during webhook verification. We choose this value.
    whatsapp_verify_token: str | None = None
    # Used to validate the X-Hub-Signature-256 header on every delivery.
    whatsapp_app_secret: str | None = None
    whatsapp_api_version: str = "v21.0"
    whatsapp_graph_url: str = "https://graph.facebook.com"
    # Meta closes the free-form window 24h after the lead's last message; outside it
    # only approved template messages may be sent.
    whatsapp_service_window_hours: int = 24

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def sync_database_url(self) -> str:
        """Alembic and psql-style tooling need the sync driver."""
        return self.database_url.replace("+asyncpg", "").replace("+aiosqlite", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
