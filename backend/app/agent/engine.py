"""The conversation engine.

One inbound message in, one reply out, with tool calls in between. Deliberately
channel-agnostic: WhatsApp (M3) and the CLI harness both drive the same entry
point.

Two rules are enforced in code rather than left to the model, because both are
compliance obligations that must not depend on the model's judgement:

* opt-out is detected and honoured before the model is called at all;
* a budget over the agent's threshold escalates whatever the model decides.

The tool loop's intermediate turns (tool_use / tool_result) are not persisted —
only the lead-visible text is. Transcripts stay readable, and a resumed
conversation re-derives state from the Lead row rather than replaying tool calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import prompts
from app.agent.llm import LLMClient, LLMError
from app.agent.optout import is_opt_out, opt_out_confirmation
from app.agent.tools import TOOL_SCHEMAS, ToolContext, ToolError, dispatch
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, mask_phone
from app.models import Agent, Conversation, Lead, Listing, Message
from app.models.enums import (
    Channel,
    ConsentStatus,
    ConversationStatus,
    Language,
    LeadStatus,
    ListingStatus,
    MessageDirection,
    MessageRole,
    MessageStatus,
)
from app.services.followups import cancel_pending, schedule_next
from app.services.google_calendar import GoogleCalendarClient

log = get_logger(__name__)

PROMPT_NAME = "qualification_system"

LANGUAGE_INSTRUCTIONS = {
    Language.ENGLISH: (
        "Reply in the language the lead writes in. They have used English so far. "
        "If they switch to Hindi or Gujarati, switch with them — including "
        "Romanised Hindi/Gujarati, which you should mirror rather than converting "
        "to Devanagari or Gujarati script."
    ),
    Language.HINDI: (
        "Reply in Hindi, matching their script — if they write Romanised Hindi "
        "('ghar chahiye'), reply the same way rather than in Devanagari. Switch "
        "language if they do."
    ),
    Language.GUJARATI: (
        "Reply in Gujarati, matching their script — if they write Romanised "
        "Gujarati, reply the same way. Switch language if they do."
    ),
}

FALLBACK_REPLY = {
    Language.ENGLISH: (
        "Sorry, I'm having trouble on my end. I've let {agent} know and they'll "
        "get back to you shortly."
    ),
    Language.HINDI: (
        "क्षमा करें, मुझे कुछ तकनीकी दिक्कत हो रही है। मैंने {agent} को बता दिया है, वे जल्द ही आपसे संपर्क करेंगे।"
    ),
    Language.GUJARATI: (
        "માફ કરશો, મને થોડી ટેકનિકલ મુશ્કેલી થઈ રહી છે. મેં {agent}ને જાણ કરી છે, તેઓ ટૂંક સમયમાં તમારો સંપર્ક કરશે."
    ),
}


@dataclass
class EngineResult:
    reply: str
    conversation: Conversation
    escalated: bool = False
    escalation_reason: str | None = None
    opted_out: bool = False
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def needs_human(self) -> bool:
        return self.escalated or self.error is not None


class ConversationEngine:
    def __init__(
        self,
        session: AsyncSession,
        llm: LLMClient,
        settings: Settings | None = None,
        calendar: GoogleCalendarClient | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._settings = settings or get_settings()
        # Optional: agents without a connected calendar book into the database only.
        self._calendar = calendar

    # ------------------------------------------------------------------ public

    async def handle_inbound(
        self,
        *,
        lead: Lead,
        agent: Agent,
        text: str,
        channel: Channel = Channel.CLI,
        external_id: str | None = None,
        now: datetime | None = None,
        recorded_inbound: Message | None = None,
    ) -> EngineResult:
        """Process one inbound message and return the reply to send.

        `recorded_inbound` is for callers that already persisted the message — the
        WhatsApp webhook claims it synchronously so a Meta retry is deduplicated
        before the slow model turn begins. Pass it to avoid a duplicate row.
        """
        now = now or datetime.now(UTC)
        conversation = (
            await self._get_or_create_conversation(lead, channel)
            if recorded_inbound is None
            else await self._session.get_one(Conversation, recorded_inbound.conversation_id)
        )

        if recorded_inbound is None:
            await self._record_message(
                conversation,
                role=MessageRole.LEAD,
                direction=MessageDirection.INBOUND,
                text=text,
                channel=channel,
                external_id=external_id,
                status=MessageStatus.RECEIVED,
                sent_at=now,
            )
        # When the lead actually wrote, not when we got round to processing it —
        # WhatsApp's 24h free-form window is measured from their message, so using
        # the processing time here would let a delayed turn send outside the window.
        lead.last_inbound_at = (
            recorded_inbound.sent_at
            if recorded_inbound is not None and recorded_inbound.sent_at is not None
            else now
        )
        if lead.consent_status is ConsentStatus.UNKNOWN:
            # Messaging us first is opt-in for the ensuing conversation.
            lead.consent_status = ConsentStatus.OPTED_IN

        if is_opt_out(text):
            return await self._handle_opt_out(lead, conversation, channel, now)

        # Check the lead as well as the conversation: an agent can claim a lead
        # before it has a conversation, and a handed-off lead whose conversation
        # was closed and recreated must not quietly get the assistant back.
        if (
            conversation.status is ConversationStatus.HUMAN_TAKEOVER
            or lead.status is LeadStatus.HANDED_OFF
        ):
            conversation.status = ConversationStatus.HUMAN_TAKEOVER
            # A human owns this thread; record the message and stay quiet.
            await cancel_pending(self._session, lead.id, "human took over the conversation")
            await self._session.flush()
            return EngineResult(
                reply="",
                conversation=conversation,
                escalated=True,
                escalation_reason="human_takeover",
            )

        ctx = ToolContext(
            session=self._session,
            agent=agent,
            lead=lead,
            conversation=conversation,
            inbound_message_count=await self._inbound_count(conversation),
            now=now,
            calendar=self._calendar,
        )

        await self._apply_budget_escalation(ctx)

        try:
            reply, tool_calls = await self._run_turn(ctx, conversation, channel)
        except LLMError as exc:
            log.error("conversation turn failed for lead %s: %s", lead.id, exc)
            reply = FALLBACK_REPLY[lead.language].format(agent=agent.name)
            await self._escalate_for_failure(ctx, str(exc))
            await self._send_and_record(conversation, lead, reply, channel, now)
            await schedule_next(
                session=self._session, lead=lead, settings=self._settings, now=now, channel=channel
            )
            return EngineResult(
                reply=reply,
                conversation=conversation,
                escalated=True,
                escalation_reason="engine_error",
                error=str(exc),
            )

        if ctx.calendar_sync_failed:
            await self._escalate_for_failure(ctx, ctx.calendar_sync_failed)

        await self._send_and_record(conversation, lead, reply, channel, now)
        # Re-arm the follow-up cadence from this reply. schedule_next cancels any
        # pending nudge first and returns None when one would be inappropriate —
        # booked, handed off or opted out — so this one call covers every outcome.
        await schedule_next(
            session=self._session, lead=lead, settings=self._settings, now=now, channel=channel
        )
        return EngineResult(
            reply=reply,
            conversation=conversation,
            escalated=ctx.escalated,
            escalation_reason=ctx.escalation_reason,
            tool_calls=tool_calls,
        )

    # ------------------------------------------------------------------ turn

    async def _run_turn(
        self, ctx: ToolContext, conversation: Conversation, channel: Channel
    ) -> tuple[str, list[str]]:
        system = await self.build_system_prompt(ctx, channel)
        messages = await self._history_as_messages(conversation)
        tool_calls: list[str] = []

        response = None
        for _ in range(self._settings.max_tool_iterations):
            response = await self._llm.complete(
                system=system, messages=messages, tools=TOOL_SCHEMAS
            )

            if response.stop_reason == "refusal":
                log.warning(
                    "model refused turn for lead %s (category=%s)",
                    ctx.lead.id,
                    response.refusal_category,
                )
                await self._escalate_for_failure(ctx, "model declined to answer")
                return (
                    FALLBACK_REPLY[ctx.lead.language].format(agent=ctx.agent.name),
                    tool_calls,
                )

            if not response.tool_uses:
                break

            messages.append({"role": "assistant", "content": response.content_for_history})
            results = []
            for tool_use in response.tool_uses:
                tool_calls.append(tool_use.name)
                results.append(await self._run_tool(ctx, tool_use))
            messages.append({"role": "user", "content": results})
        else:
            log.warning(
                "tool loop hit %s iterations for lead %s",
                self._settings.max_tool_iterations,
                ctx.lead.id,
            )

        reply = (response.text if response else "").strip()
        if not reply:
            reply = FALLBACK_REPLY[ctx.lead.language].format(agent=ctx.agent.name)
            await self._escalate_for_failure(ctx, "model returned no reply text")
        return reply, tool_calls

    async def _run_tool(self, ctx: ToolContext, tool_use: Any) -> dict[str, Any]:
        try:
            payload = await dispatch(ctx, tool_use.name, tool_use.input)
            is_error = False
        except ToolError as exc:
            payload = {"error": str(exc)}
            is_error = True
        except Exception as exc:  # noqa: BLE001 - never break the turn on a tool bug
            log.exception("tool %s crashed for lead %s", tool_use.name, ctx.lead.id)
            payload = {"error": f"{tool_use.name} is temporarily unavailable"}
            is_error = True
            _ = exc

        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": json.dumps(payload, default=str),
            "is_error": is_error,
        }

    # --------------------------------------------------------------- prompting

    async def build_system_prompt(self, ctx: ToolContext, channel: Channel) -> str:
        agent, lead = ctx.agent, ctx.lead
        threshold = agent.escalation_budget_threshold or self._settings.escalation_budget_threshold
        return prompts.render(
            PROMPT_NAME,
            agent_name=agent.name,
            brokerage_name=agent.brokerage_name or agent.name,
            agent_phone_masked=mask_phone(agent.phone),
            channel=channel.value,
            language_instruction=LANGUAGE_INSTRUCTIONS[lead.language],
            escalation_threshold=f"₹{threshold:,}",
            working_hours=self._format_working_hours(agent),
            timezone=agent.timezone,
            today=ctx.now.strftime("%A %d %B %Y"),
            lead_profile=self._format_lead_profile(lead),
            listing_summary=await self._format_listing_summary(agent),
            tone_instructions=self._format_tone(agent),
        )

    @staticmethod
    def _format_tone(agent: Agent) -> str:
        """The agent's own voice guidance, set during onboarding (M7)."""
        tone = (agent.tone_instructions or "").strip()
        if not tone:
            return (
                "No specific tone was set for this agent, so keep it warm, plain and professional."
            )
        return f"{agent.name} asks that you write like this:\n\n{tone}"

    @staticmethod
    def _format_working_hours(agent: Agent) -> str:
        parts = [
            f"{day} {hours[0]}-{hours[1]}"
            for day, hours in agent.working_hours.items()
            if isinstance(hours, list) and len(hours) == 2
        ]
        return ", ".join(parts) if parts else "not set"

    @staticmethod
    def _format_lead_profile(lead: Lead) -> str:
        def money(value: Decimal | None) -> str | None:
            return f"₹{value:,.0f}" if value is not None else None

        fields = {
            "Name": lead.name,
            "Budget": " - ".join(filter(None, [money(lead.budget_min), money(lead.budget_max)]))
            or None,
            "Preferred locations": ", ".join(lead.preferred_locations) or None,
            "Property type": lead.property_type.value if lead.property_type else None,
            "BHK": lead.bhk,
            "Timeline": f"{lead.timeline_months} months" if lead.timeline_months else None,
            "Loan pre-approved": lead.loan_preapproved,
            "Purpose": lead.purpose.value if lead.purpose.value != "unknown" else None,
            "Willing to visit": lead.site_visit_willing,
            "Notes": lead.notes,
        }
        known = [f"- {k}: {v}" for k, v in fields.items() if v is not None]
        return "\n".join(known) if known else "- Nothing yet; this is a fresh enquiry."

    async def _format_listing_summary(self, agent: Agent) -> str:
        result = await self._session.execute(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.agent_id == agent.id,
                Listing.is_active.is_(True),
                Listing.status == ListingStatus.AVAILABLE,
            )
        )
        count = result.scalar_one()
        if not count:
            return (
                "This agent has no active listings right now. Qualify the enquiry and "
                "book a call rather than discussing specific properties."
            )
        return (
            f"This agent has {count} active listing(s). Use get_listing_details to see "
            "them — do not guess at what is in stock."
        )

    # ------------------------------------------------------------- persistence

    async def _get_or_create_conversation(self, lead: Lead, channel: Channel) -> Conversation:
        result = await self._session.execute(
            select(Conversation)
            .where(
                Conversation.lead_id == lead.id,
                Conversation.channel == channel,
                Conversation.status != ConversationStatus.CLOSED,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            conversation = Conversation(lead_id=lead.id, channel=channel)
            self._session.add(conversation)
            await self._session.flush()
        return conversation

    async def _record_message(
        self,
        conversation: Conversation,
        *,
        role: MessageRole,
        direction: MessageDirection,
        text: str,
        channel: Channel,
        status: MessageStatus,
        external_id: str | None = None,
        sent_at: datetime | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation.id,
            role=role,
            direction=direction,
            channel=channel,
            status=status,
            content=text,
            external_id=external_id,
            sent_at=sent_at,
        )
        self._session.add(message)
        conversation.last_message_at = sent_at or datetime.now(UTC)
        await self._session.flush()
        return message

    async def _history_as_messages(self, conversation: Conversation) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at, Message.id)
        )
        messages: list[dict[str, Any]] = []
        for message in result.scalars().all():
            role = "user" if message.direction is MessageDirection.INBOUND else "assistant"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n{message.content}"
            else:
                messages.append({"role": role, "content": message.content})

        # The API requires the first turn to be from the user.
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        return messages

    async def _inbound_count(self, conversation: Conversation) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.direction == MessageDirection.INBOUND,
            )
        )
        return int(result.scalar_one())

    async def _send_and_record(
        self,
        conversation: Conversation,
        lead: Lead,
        reply: str,
        channel: Channel,
        now: datetime,
    ) -> None:
        if not reply:
            return
        await self._record_message(
            conversation,
            role=MessageRole.ASSISTANT,
            direction=MessageDirection.OUTBOUND,
            text=reply,
            channel=channel,
            status=MessageStatus.PENDING,
            sent_at=now,
        )
        lead.last_outbound_at = now
        await self._session.flush()

    # ------------------------------------------------------------- hard rules

    async def _handle_opt_out(
        self, lead: Lead, conversation: Conversation, channel: Channel, now: datetime
    ) -> EngineResult:
        lead.consent_status = ConsentStatus.OPTED_OUT
        lead.opted_out_at = now
        lead.status = LeadStatus.OPTED_OUT
        conversation.status = ConversationStatus.CLOSED

        await cancel_pending(self._session, lead.id, "lead opted out")

        reply = opt_out_confirmation(lead.language.value)
        await self._send_and_record(conversation, lead, reply, channel, now)
        log.info("lead %s (%s) opted out", lead.id, mask_phone(lead.phone))
        return EngineResult(reply=reply, conversation=conversation, opted_out=True)

    async def _apply_budget_escalation(self, ctx: ToolContext) -> None:
        """High-value leads go to the human regardless of what the model decides."""
        threshold = (
            ctx.agent.escalation_budget_threshold or self._settings.escalation_budget_threshold
        )
        budget = ctx.lead.budget_max or ctx.lead.budget_min
        if budget is None or budget <= threshold or ctx.lead.status is LeadStatus.HANDED_OFF:
            return
        await dispatch(
            ctx,
            "escalate_to_human",
            {
                "reason": f"Budget ₹{budget:,.0f} is above the ₹{threshold:,} threshold",
                "urgency": "high",
            },
        )

    async def _escalate_for_failure(self, ctx: ToolContext, reason: str) -> None:
        if ctx.escalated:
            return
        try:
            await dispatch(ctx, "escalate_to_human", {"reason": reason, "urgency": "high"})
        except Exception:  # noqa: BLE001 - already on the failure path
            log.exception("failed to escalate lead %s after error", ctx.lead.id)
