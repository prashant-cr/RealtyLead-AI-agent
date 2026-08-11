"""A scripted LLM stand-in.

Lets the full engine — tool dispatch, persistence, escalation, history building —
run in tests with no API key and no network. Scripted turns are consumed in
order; each is either plain text or a set of tool calls followed by the next turn.
"""

from __future__ import annotations

from collections.abc import Mapping
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


class FakeRedis:
    """An in-memory stand-in for the slice of Redis this project uses.

    Written by hand rather than pulling in `fakeredis` because the surface is
    small (`app.core.redis.RedisLike`) and a real dependency would have to be
    justified — see CLAUDE.md on adding dependencies. Keeping it hand-rolled also
    means the fake cannot quietly support commands the production code never
    issues.

    Time is explicit: `advance()` moves a virtual clock so tests can make an
    entry look stale, or a rate-limit window look expired, without sleeping.
    """

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        # (stream, group) -> {"delivered": set[str], "pending": {id: (consumer, when)}}
        self.groups: dict[tuple[str, str], dict[str, Any]] = {}
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, float] = {}
        self.now = 1_000.0
        self._seq = 0
        self.closed = False
        # Set to an exception to make every call fail, for the fail-open paths.
        self.fail_with: Exception | None = None

    # --- test helpers -------------------------------------------------
    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def _expire_due(self) -> None:
        for key, at in list(self.expiries.items()):
            if at <= self.now:
                self.counters.pop(key, None)
                self.expiries.pop(key, None)

    # --- connection ---------------------------------------------------
    async def ping(self) -> bool:
        self._check()
        return True

    async def aclose(self) -> None:
        self.closed = True

    # --- streams ------------------------------------------------------
    async def xadd(self, name: str, fields: Mapping[str, Any], maxlen: int | None = None) -> str:
        self._check()
        self._seq += 1
        entry_id = f"1-{self._seq}"
        entries = self.streams.setdefault(name, [])
        entries.append((entry_id, {k: str(v) for k, v in fields.items()}))
        if maxlen is not None and len(entries) > maxlen:
            del entries[: len(entries) - maxlen]
        return entry_id

    async def xgroup_create(
        self, name: str, groupname: str, id: str = "$", mkstream: bool = False
    ) -> bool:
        self._check()
        if (name, groupname) in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        if mkstream:
            self.streams.setdefault(name, [])
        self.groups[(name, groupname)] = {"delivered": set(), "pending": {}}
        return True

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[Any]:
        self._check()
        out: list[Any] = []
        for stream in streams:
            group = self.groups.get((stream, groupname))
            if group is None:
                raise RuntimeError("NOGROUP No such consumer group")
            fresh = [
                (eid, fields)
                for eid, fields in self.streams.get(stream, [])
                if eid not in group["delivered"]
            ]
            if count is not None:
                fresh = fresh[:count]
            for eid, _fields in fresh:
                group["delivered"].add(eid)
                group["pending"][eid] = (consumername, self.now)
            if fresh:
                out.append([stream, fresh])
        return out

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        self._check()
        group = self.groups.get((name, groupname))
        if group is None:
            return 0
        return sum(1 for eid in ids if group["pending"].pop(eid, None) is not None)

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str = "0-0",
        count: int | None = None,
    ) -> list[Any]:
        self._check()
        group = self.groups.get((name, groupname))
        if group is None:
            return ["0-0", [], []]

        by_id = dict(self.streams.get(name, []))
        claimed: list[tuple[str, dict[str, str]]] = []
        for eid, (_consumer, delivered_at) in sorted(group["pending"].items()):
            if (self.now - delivered_at) * 1000 < min_idle_time:
                continue
            if eid in by_id:
                claimed.append((eid, by_id[eid]))
                group["pending"][eid] = (consumername, self.now)
            if count is not None and len(claimed) >= count:
                break
        return ["0-0", claimed, []]

    async def xlen(self, name: str) -> int:
        self._check()
        return len(self.streams.get(name, []))

    async def xpending(self, name: str, groupname: str) -> dict[str, Any]:
        self._check()
        group = self.groups.get((name, groupname))
        return {"pending": len(group["pending"]) if group else 0}

    # --- counters -----------------------------------------------------
    async def incr(self, name: str) -> int:
        self._check()
        self._expire_due()
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    async def expire(self, name: str, time: int) -> bool:
        self._check()
        self.expiries[name] = self.now + time
        return True

    async def ttl(self, name: str) -> int:
        self._check()
        self._expire_due()
        if name not in self.counters:
            return -2
        if name not in self.expiries:
            return -1
        return int(self.expiries[name] - self.now)

    async def delete(self, *names: str) -> int:
        self._check()
        removed = 0
        for name in names:
            removed += 1 if self.counters.pop(name, None) is not None else 0
            self.expiries.pop(name, None)
        return removed
