"""Anthropic client wrapper.

Normalises the SDK's response into a small shape the engine can reason about, so
the engine is testable with a fake and does not spread SDK types through the
codebase. `content_for_history` is passed back to the API verbatim — thinking
blocks in particular must be echoed unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import anthropic
from anthropic.types import MessageParam, OutputConfigParam, TextBlockParam, ToolParam

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    stop_reason: str | None
    text_blocks: list[TextBlock] = field(default_factory=list)
    tool_uses: list[ToolUseBlock] = field(default_factory=list)
    # Opaque: handed straight back as the assistant turn on the next request.
    content_for_history: Any = None
    refusal_category: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.text_blocks if b.text).strip()


class LLMClient(Protocol):
    """What the conversation engine needs from a model provider."""

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse: ...


class LLMError(RuntimeError):
    """Raised when the model call fails in a way the engine cannot recover from."""


class AnthropicLLM:
    """Claude-backed implementation.

    The SDK handles retries with exponential backoff (429/5xx/connection errors)
    and per-request timeouts; we only configure them and translate failures.
    """

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        if client is None:
            if not settings.anthropic_api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY is not set — the conversation engine cannot start"
                )
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.http_timeout_seconds * 3,
                max_retries=settings.http_max_retries,
            )
        self._client = client
        self._settings = settings

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        try:
            # The system prompt is stable across a conversation's turns, so cache it.
            system_blocks: list[TextBlockParam] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
            response = await self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=self._settings.anthropic_max_tokens,
                output_config=cast(OutputConfigParam, {"effort": self._settings.anthropic_effort}),
                system=system_blocks,
                messages=cast(list[MessageParam], messages),
                tools=cast(list[ToolParam], tools),
            )
        except anthropic.RateLimitError as exc:
            log.warning("anthropic rate limited: %s", exc.message)
            raise LLMError("model provider rate limited") from exc
        except anthropic.APIStatusError as exc:
            log.error("anthropic API error %s: %s", exc.status_code, exc.message)
            raise LLMError(f"model provider error ({exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            log.error("anthropic connection error: %s", type(exc).__name__)
            raise LLMError("could not reach the model provider") from exc

        return self._normalise(response)

    @staticmethod
    def _normalise(response: Any) -> LLMResponse:
        text_blocks: list[TextBlock] = []
        tool_uses: list[ToolUseBlock] = []
        for block in response.content:
            if block.type == "text":
                text_blocks.append(TextBlock(block.text))
            elif block.type == "tool_use":
                tool_uses.append(ToolUseBlock(block.id, block.name, dict(block.input)))

        category = None
        if response.stop_reason == "refusal" and getattr(response, "stop_details", None):
            category = getattr(response.stop_details, "category", None)

        return LLMResponse(
            stop_reason=response.stop_reason,
            text_blocks=text_blocks,
            tool_uses=tool_uses,
            content_for_history=response.content,
            refusal_category=category,
        )
