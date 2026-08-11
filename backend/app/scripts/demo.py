"""End-to-end demo with no external accounts required.

Plays a complete lead journey against the real database using a scripted model
and an in-memory WhatsApp channel, so anyone can see what the product does
without an Anthropic key, a Meta app or a Google account:

    make demo

What it shows: an enquiry arrives, gets qualified, is scored with reasons,
books a site visit, and has a follow-up queued. Then a second lead opts out and
is honoured instantly.

The *model's* replies are scripted — this demonstrates the machinery, not the
assistant's writing. For that you need a real key and `make chat`.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.engine import ConversationEngine
from app.agent.llm import LLMResponse, TextBlock, ToolUseBlock
from app.channels.memory import InMemoryChannel
from app.core.config import get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.models import Agent, Conversation, FollowUpTask, Lead, Listing, Message
from app.models.enums import Channel, FollowUpStatus
from app.services.scheduling import find_available_slots
from app.workers.followup_worker import run_once

DIM, BOLD, CYAN, GREEN, YELLOW, RESET = (
    "\033[2m",
    "\033[1m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)

BUYER_PHONE = "+919000000101"
LEAVER_PHONE = "+919000000102"

NULL_PROFILE = dict.fromkeys(
    [
        "name",
        "budget_min",
        "budget_max",
        "preferred_locations",
        "property_type",
        "bhk",
        "timeline_months",
        "loan_preapproved",
        "purpose",
        "site_visit_willing",
        "notes",
    ]
)
NULL_SEARCH = dict.fromkeys(
    ["listing_id", "city", "locality", "property_type", "bhk", "budget_min", "budget_max"]
)


def text_turn(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        text_blocks=[TextBlock(text)],
        content_for_history=[{"type": "text", "text": text}],
    )


def tool_turn(*calls: tuple[str, dict[str, Any]]) -> LLMResponse:
    uses = [
        ToolUseBlock(id=f"toolu_{index}", name=name, input=args)
        for index, (name, args) in enumerate(calls)
    ]
    return LLMResponse(
        stop_reason="tool_use",
        tool_uses=uses,
        content_for_history=[
            {"type": "tool_use", "id": u.id, "name": u.name, "input": u.input} for u in uses
        ],
    )


class ScriptedLLM:
    """Replays canned turns so the demo needs no API key.

    Deliberately local to this script rather than imported from `tests/`: nothing
    shipped in `app/` should depend on test scaffolding.
    """

    def __init__(self, *turns: LLMResponse) -> None:
        self._turns = list(turns)

    async def complete(self, **_kwargs: Any) -> LLMResponse:
        return self._turns.pop(0) if self._turns else text_turn("(nothing scripted)")


def _when(moment: datetime) -> str:
    return moment.astimezone().strftime("%A at %I:%M %p").replace(" 0", " ")


def heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}\n{DIM}{'─' * len(text)}{RESET}")


def said(who: str, text: str, tools: list[str] | None = None) -> None:
    colour = CYAN if who == "lead" else BOLD
    if tools:
        print(f"{DIM}      [tools: {', '.join(tools)}]{RESET}")
    print(f"{colour}{who:>9}{RESET} │ {text}")


async def reset(session: AsyncSession) -> None:
    """Remove anything this demo created previously, so it is repeatable."""
    lead_ids = (
        (await session.execute(select(Lead.id).where(Lead.phone.in_([BUYER_PHONE, LEAVER_PHONE]))))
        .scalars()
        .all()
    )
    if lead_ids:
        conv_ids = (
            (
                await session.execute(
                    select(Conversation.id).where(Conversation.lead_id.in_(lead_ids))
                )
            )
            .scalars()
            .all()
        )
        if conv_ids:
            await session.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
            await session.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
        await session.execute(delete(FollowUpTask).where(FollowUpTask.lead_id.in_(lead_ids)))
        from app.models import Appointment

        await session.execute(delete(Appointment).where(Appointment.lead_id.in_(lead_ids)))
        await session.execute(delete(Lead).where(Lead.id.in_(lead_ids)))
    await session.commit()


async def run(reset_first: bool) -> int:
    settings = get_settings()
    configure_logging("ERROR")  # the demo narrates itself; logs would be noise

    async with get_sessionmaker()() as session:
        agent = (await session.execute(select(Agent).order_by(Agent.created_at))).scalars().first()
        if agent is None:
            print("No agent found. Run `make migrate && make seed` first.")
            return 1
        listing = (
            (await session.execute(select(Listing).where(Listing.agent_id == agent.id)))
            .scalars()
            .first()
        )
        if listing is None:
            print("No listings found. Run `make seed` first.")
            return 1

        if reset_first:
            await reset(session)

        now = datetime.now(UTC)
        channel = InMemoryChannel(Channel.WHATSAPP)

        print(f"\n{BOLD}RealtyLead — end-to-end demo{RESET}")
        print(
            f"{DIM}agent: {agent.name} ({agent.brokerage_name}) · "
            f"model replies are scripted, everything else is real{RESET}"
        )

        # ------------------------------------------------------------------ buyer
        heading("1. A serious buyer enquires")

        buyer = Lead(agent_id=agent.id, phone=BUYER_PHONE, name="Priya Shah", source="demo")
        session.add(buyer)
        await session.flush()

        slots = await find_available_slots(session, agent, search_days=7, now=now)
        if not slots:
            print("No availability in the agent's working hours — check `make seed`.")
            return 1
        chosen = slots[0]

        script = ScriptedLLM(
            tool_turn(("get_listing_details", {**NULL_SEARCH, "locality": listing.locality})),
            text_turn(
                f"Hi Priya! I'm {agent.name}'s assistant at {agent.brokerage_name}. Yes, the "
                f"{listing.title} is available. What budget are you working with?"
            ),
            tool_turn(
                (
                    "update_lead_profile",
                    {
                        **NULL_PROFILE,
                        "name": "Priya Shah",
                        "budget_min": 6000000,
                        "budget_max": 9000000,
                        "timeline_months": 2,
                        "loan_preapproved": True,
                        "preferred_locations": [listing.locality or listing.city],
                        "bhk": listing.bhk,
                    },
                )
            ),
            tool_turn(("score_lead", {})),
            text_turn("That fits nicely. Would you like to see it in person this week?"),
            tool_turn(("check_availability", {"appointment_type": "site_visit", "search_days": 7})),
            text_turn(f"I can do {_when(chosen.starts_at)} — does that work?"),
            tool_turn(
                (
                    "book_appointment",
                    {
                        "starts_at": chosen.starts_at.isoformat(),
                        "appointment_type": "site_visit",
                        "listing_id": str(listing.id),
                        "notes": "Wants an east-facing unit",
                    },
                )
            ),
            text_turn(f"Booked. {agent.name} will meet you at the property — see you then!"),
        )
        engine = ConversationEngine(session, script, settings)

        conversation = [
            "Hi, is the Bopal flat still available?",
            "Around 90 lakhs. Buying in 2 months, my home loan is already approved.",
            "Yes, I'd like to see it",
            "Thursday works for me",
        ]
        for message in conversation:
            said("lead", message)
            result = await engine.handle_inbound(
                lead=buyer, agent=agent, text=message, channel=Channel.WHATSAPP, now=now
            )
            await session.commit()
            said("assistant", result.reply, result.tool_calls)

        await session.refresh(buyer)
        heading("2. What the human agent sees")
        print(f"  status      {buyer.status.value}")
        print(f"  score       {buyer.score}/100 ({buyer.temperature.value})")
        for reason in buyer.score_reasons:
            print(f"{DIM}    +{reason['points']:<3} {reason['detail']}{RESET}")

        from app.models import Appointment

        appointment = (
            (await session.execute(select(Appointment).where(Appointment.lead_id == buyer.id)))
            .scalars()
            .first()
        )
        if appointment:
            local = appointment.starts_at.astimezone()
            print(
                f"  booked      {appointment.appointment_type.value} on "
                f"{local.strftime('%a %d %b at %I:%M %p').replace(' 0', ' ')}"
            )
            print(
                f"{DIM}              calendar: "
                f"{'synced' if appointment.google_event_id else 'not connected'}{RESET}"
            )

        # -------------------------------------------------------------- follow-up
        heading("3. A quiet lead gets a nudge — and opts out")

        leaver = Lead(
            agent_id=agent.id,
            phone=LEAVER_PHONE,
            name="Amit Patel",
            source="demo",
            last_inbound_at=now - timedelta(days=2),
        )
        session.add(leaver)
        await session.flush()

        quiet_engine = ConversationEngine(
            session, ScriptedLLM(text_turn("Sure, take your time!")), settings
        )
        said("lead", "Just browsing for now, thanks")
        result = await quiet_engine.handle_inbound(
            lead=leaver,
            agent=agent,
            text="Just browsing for now, thanks",
            channel=Channel.WHATSAPP,
            now=now - timedelta(days=2),
        )
        await session.commit()
        said("assistant", result.reply)

        task = (
            (
                await session.execute(
                    select(FollowUpTask).where(
                        FollowUpTask.lead_id == leaver.id,
                        FollowUpTask.status == FollowUpStatus.SCHEDULED,
                    )
                )
            )
            .scalars()
            .first()
        )
        if task:
            print(
                f"{DIM}      nudge #{task.attempt_number} queued for "
                f"{task.scheduled_for.astimezone().strftime('%a %d %b, %I:%M %p')}{RESET}"
            )

        # Jump the clock so the worker sends it.
        report = await run_once(
            settings,
            now=now - timedelta(days=1) + timedelta(minutes=1),
            session=session,
            adapter=channel,
        )
        await session.commit()
        if channel.outbox:
            sent = channel.outbox[-1]
            print(f"{DIM}      worker: {report}{RESET}")
            said("assistant", f"{sent.text}")
            print(f"{DIM}              sent as approved template '{sent.template_name}'{RESET}")

        said("lead", "STOP")
        opt_out = await quiet_engine.handle_inbound(
            lead=leaver, agent=agent, text="STOP", channel=Channel.WHATSAPP, now=now
        )
        await session.commit()
        said("assistant", opt_out.reply)
        await session.refresh(leaver)
        print(
            f"{GREEN}      ✓ consent={leaver.consent_status.value}, "
            f"status={leaver.status.value}, all future nudges cancelled{RESET}"
        )
        print(
            f"{DIM}      (the model was never called for this turn — opt-out is enforced "
            f"in code){RESET}"
        )

        # ----------------------------------------------------------------- escalate
        heading("4. Price negotiation goes to the human")

        escalate_engine = ConversationEngine(
            session,
            ScriptedLLM(
                tool_turn(
                    ("escalate_to_human", {"reason": "Wants to negotiate price", "urgency": "high"})
                ),
                text_turn(f"That's one for {agent.name} — he'll call you shortly."),
            ),
            settings,
        )
        said("lead", "Can you do better on the price?")
        escalated = await escalate_engine.handle_inbound(
            lead=buyer, agent=agent, text="Can you do better on the price?", now=now
        )
        await session.commit()
        said("assistant", escalated.reply, escalated.tool_calls)
        print(f"{YELLOW}      ⚑ escalated: {escalated.escalation_reason}{RESET}")

        heading("Done")
        print("  Open the dashboard to see both leads, the transcripts and the score reasons:")
        print(f"    {BOLD}make token{RESET}      # then paste it at")
        print(f"    {BOLD}make dashboard{RESET}  # http://localhost:3000\n")

    await dispose_engine()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an end-to-end demo (no API keys needed).")
    parser.add_argument(
        "--keep", action="store_true", help="Keep any demo leads from a previous run."
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(reset_first=not args.keep)))


if __name__ == "__main__":
    main()
