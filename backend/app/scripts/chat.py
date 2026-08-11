"""CLI test harness — talk to the conversation engine from a terminal.

    make chat                      # uses the seeded demo agent
    make chat PHONE=+919812345678  # resume a specific lead

Needs ANTHROPIC_API_KEY in .env, plus a migrated + seeded database.
Type /quit to exit, /profile to dump what the agent has learned, /reset to start
the conversation over.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.engine import ConversationEngine
from app.agent.llm import AnthropicLLM, LLMError
from app.core.config import get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.models import Agent, Conversation, Lead, Message
from app.models.enums import Channel, Language
from app.services.google_calendar import GoogleCalendarClient

DEMO_PHONE = "+919999000001"

DIM, BOLD, CYAN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


async def _resolve_agent(session: AsyncSession, email: str | None) -> Agent:
    query = select(Agent).order_by(Agent.created_at)
    if email:
        query = query.where(Agent.email == email)
    agent = (await session.execute(query.limit(1))).scalar_one_or_none()
    if agent is None:
        raise SystemExit("No agent found. Run `make migrate && make seed` first.")
    return agent


async def _resolve_lead(session: AsyncSession, agent: Agent, phone: str, language: str) -> Lead:
    lead = (
        await session.execute(select(Lead).where(Lead.agent_id == agent.id, Lead.phone == phone))
    ).scalar_one_or_none()
    if lead is None:
        lead = Lead(
            agent_id=agent.id,
            phone=phone,
            source="cli",
            language=Language(language),
        )
        session.add(lead)
        await session.flush()
    return lead


async def _reset(session: AsyncSession, lead: Lead) -> None:
    conversations = (
        (await session.execute(select(Conversation.id).where(Conversation.lead_id == lead.id)))
        .scalars()
        .all()
    )
    if conversations:
        await session.execute(delete(Message).where(Message.conversation_id.in_(conversations)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conversations)))
    await session.commit()


def _print_profile(lead: Lead) -> None:
    rows = {
        "status": lead.status.value,
        "score": f"{lead.score} ({lead.temperature.value})",
        "budget": f"{lead.budget_min} - {lead.budget_max}",
        "locations": lead.preferred_locations,
        "type / bhk": f"{lead.property_type} / {lead.bhk}",
        "timeline": lead.timeline_months,
        "loan pre-approved": lead.loan_preapproved,
        "purpose": lead.purpose.value,
        "site visit": lead.site_visit_willing,
        "consent": lead.consent_status.value,
    }
    print(f"{DIM}--- lead profile ---{RESET}")
    for key, value in rows.items():
        print(f"{DIM}  {key:<18} {value}{RESET}")
    for reason in lead.score_reasons:
        print(f"{DIM}  · {reason['factor']}: +{reason['points']} — {reason['detail']}{RESET}")


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging("WARNING" if not args.verbose else "INFO")

    try:
        llm = AnthropicLLM(settings)
    except LLMError as exc:
        print(f"{YELLOW}{exc}{RESET}", file=sys.stderr)
        return 2

    async with get_sessionmaker()() as session:
        agent = await _resolve_agent(session, args.agent_email)
        lead = await _resolve_lead(session, agent, args.phone, args.language)
        if args.reset:
            await _reset(session, lead)
        await session.commit()

        calendar = GoogleCalendarClient(settings) if agent.google_refresh_token else None
        engine = ConversationEngine(session, llm, settings, calendar=calendar)

        print(f"{BOLD}RealtyLead — chatting with {agent.name}'s assistant{RESET}")
        print(
            f"{DIM}lead {args.phone} · model {settings.anthropic_model} "
            f"· effort {settings.anthropic_effort}{RESET}"
        )
        print(f"{DIM}/quit  /profile  /reset{RESET}\n")

        while True:
            try:
                # to_thread, not bare input(): a blocking read would stall the event loop.
                text = (await asyncio.to_thread(input, f"{CYAN}you ›{RESET} ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not text:
                continue
            if text in {"/quit", "/exit"}:
                break
            if text == "/profile":
                await session.refresh(lead)
                _print_profile(lead)
                continue
            if text == "/reset":
                await _reset(session, lead)
                print(f"{DIM}conversation cleared{RESET}")
                continue

            result = await engine.handle_inbound(
                lead=lead,
                agent=agent,
                text=text,
                channel=Channel.CLI,
                now=datetime.now(UTC),
            )
            await session.commit()

            if result.tool_calls:
                print(f"{DIM}   [tools: {', '.join(result.tool_calls)}]{RESET}")
            if result.reply:
                print(f"{BOLD}bot ›{RESET} {result.reply}")
            if result.escalated:
                print(f"{YELLOW}   ⚑ escalated to {agent.name}: {result.escalation_reason}{RESET}")
            if result.opted_out:
                print(f"{YELLOW}   ⚑ lead opted out — conversation closed{RESET}")
                break
            print()

        if calendar is not None:
            await calendar.close()

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the RealtyLead conversation engine.")
    parser.add_argument("--phone", default=DEMO_PHONE, help="Lead phone number to converse as.")
    parser.add_argument("--agent-email", default=None, help="Which agent to test against.")
    parser.add_argument(
        "--language", default="en", choices=["en", "hi", "gu"], help="Lead's starting language."
    )
    parser.add_argument("--reset", action="store_true", help="Clear the conversation first.")
    parser.add_argument("--verbose", action="store_true", help="Show INFO logs.")
    args = parser.parse_args()

    try:
        code = asyncio.run(_run_and_close(args))
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


async def _run_and_close(args: argparse.Namespace) -> int:
    try:
        return await run(args)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    main()
