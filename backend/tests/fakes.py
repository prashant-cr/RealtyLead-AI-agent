"""A scripted LLM stand-in.

Lets the full engine — tool dispatch, persistence, escalation, history building —
run in tests with no API key and no network. Scripted turns are consumed in
order; each is either plain text or a set of tool calls followed by the next turn.
"""

from __future__ import annotations

from typing import Any

from app.agent.llm import LLMResponse, TextBlock, ToolUseBlock


def text_turn(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        text_blocks=[TextBlock(text)],
        content_for_history=[{"type": "text", "text": text}],
    )


def tool_turn(*calls: tuple[str, dict[str, Any]], text: str = "") -> LLMResponse:
    tool_uses = [
        ToolUseBlock(id=f"toolu_{index}", name=name, input=args)
        for index, (name, args) in enumerate(calls)
    ]
    return LLMResponse(
        stop_reason="tool_use",
        text_blocks=[TextBlock(text)] if text else [],
        tool_uses=tool_uses,
        content_for_history=[
            {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}
            for tu in tool_uses
        ],
    )


def refusal_turn(category: str = "cyber") -> LLMResponse:
    return LLMResponse(stop_reason="refusal", content_for_history=[], refusal_category=category)


class FakeLLM:
    """Replays scripted responses and records what it was asked."""

    def __init__(self, *turns: LLMResponse) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if not self.turns:
            return text_turn("(no scripted turn left)")
        return self.turns.pop(0)

    @property
    def last_system(self) -> str:
        return str(self.calls[-1]["system"])

    @property
    def last_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = self.calls[-1]["messages"]
        return messages


class ExplodingLLM:
    """Raises the given exception on every call."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def complete(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        raise self._error
