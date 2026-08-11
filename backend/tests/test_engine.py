from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.engine import ConversationEngine
from app.agent.llm import LLMError
from app.models import Agent, Lead, Message
from app.models.enums import (
    ConsentStatus,
    ConversationStatus,
    Language,
    LeadStatus,
    MessageDirection,
)
from tests.factories import make_agent, make_lead, make_listing
from tests.fakes import ExplodingLLM, FakeLLM, refusal_turn, text_turn, tool_turn

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 12, 6, 0, tzinfo=IST).astimezone(UTC)

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


async def setup(session: AsyncSession, **lead_overrides: object) -> tuple[Agent, Lead]:
    agent = make_agent()
    session.add(agent)
    await session.flush()
    session.add(make_listing(agent, price=Decimal("8500000"), bhk=3, locality="Bopal"))
    lead = make_lead(agent, **lead_overrides)
    session.add(lead)
    await session.flush()
    return agent, lead


async def test_simple_turn_replies_and_persists_both_messages(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    llm = FakeLLM(text_turn("Hi! I'm Rohan's assistant. What's your budget?"))
    engine = ConversationEngine(session, llm)

    result = await engine.handle_inbound(
        lead=lead, agent=agent, text="Is the Bopal flat available?", now=NOW
    )

    assert result.reply.startswith("Hi!")
    assert result.escalated is False
    messages = (await session.execute(select(Message))).scalars().all()
    assert [m.direction for m in messages] == [
        MessageDirection.INBOUND,
        MessageDirection.OUTBOUND,
    ]
    assert lead.last_inbound_at == NOW
    assert lead.last_outbound_at == NOW


async def test_inbound_message_records_consent(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    engine = ConversationEngine(session, FakeLLM(text_turn("Hello!")))

    await engine.handle_inbound(lead=lead, agent=agent, text="Hi", now=NOW)

    assert lead.consent_status is ConsentStatus.OPTED_IN


async def test_tool_results_feed_back_into_the_reply(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    llm = FakeLLM(
        tool_turn(
            (
                "get_listing_details",
                {
                    **dict.fromkeys(
                        [
                            "listing_id",
                            "city",
                            "locality",
                            "property_type",
                            "bhk",
                            "budget_min",
                            "budget_max",
                        ]
                    ),
                    "locality": "Bopal",
                },
            )
        ),
        text_turn("Yes — the 3 BHK in Bopal is available at ₹85 lakhs."),
    )
    engine = ConversationEngine(session, llm)

    result = await engine.handle_inbound(lead=lead, agent=agent, text="Anything in Bopal?", now=NOW)

    assert result.tool_calls == ["get_listing_details"]
    assert "85 lakhs" in result.reply
    # Second call carried the assistant turn plus the tool_result back to the model.
    second_turn = llm.calls[1]["messages"]
    assert second_turn[-1]["content"][0]["type"] == "tool_result"


async def test_profile_learned_mid_conversation_is_saved(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    llm = FakeLLM(
        tool_turn(
            (
                "update_lead_profile",
                {
                    **NULL_PROFILE,
                    "budget_max": 9000000,
                    "timeline_months": 2,
                    "loan_preapproved": True,
                },
            ),
        ),
        tool_turn(("score_lead", {})),
        text_turn("Great — shall I book you a call with Rohan?"),
    )
    engine = ConversationEngine(session, llm)

    result = await engine.handle_inbound(
        lead=lead, agent=agent, text="Budget 90 lakhs, buying in 2 months, loan approved", now=NOW
    )

    assert result.tool_calls == ["update_lead_profile", "score_lead"]
    assert lead.budget_max == Decimal("9000000")
    assert lead.score > 0
    assert lead.score_reasons


async def test_opt_out_short_circuits_before_the_model_is_called(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    llm = FakeLLM(text_turn("this should never be sent"))
    engine = ConversationEngine(session, llm)

    result = await engine.handle_inbound(lead=lead, agent=agent, text="STOP", now=NOW)

    assert llm.calls == []
    assert result.opted_out is True
    assert lead.consent_status is ConsentStatus.OPTED_OUT
    assert lead.status is LeadStatus.OPTED_OUT
    assert lead.opted_out_at == NOW
    assert result.conversation.status is ConversationStatus.CLOSED
    assert result.reply


async def test_opt_out_confirmation_uses_the_leads_language(session: AsyncSession) -> None:
    agent, lead = await setup(session, language=Language.GUJARATI)
    engine = ConversationEngine(session, FakeLLM())

    result = await engine.handle_inbound(lead=lead, agent=agent, text="બંધ કરો", now=NOW)

    assert result.opted_out is True
    assert "બંધ" in result.reply or "સમજી" in result.reply


async def test_high_budget_escalates_regardless_of_the_model(session: AsyncSession) -> None:
    agent, lead = await setup(session, budget_max=Decimal("50000000"))
    agent.escalation_budget_threshold = 20_000_000
    llm = FakeLLM(text_turn("Let me connect you with Rohan."))
    engine = ConversationEngine(session, llm)

    result = await engine.handle_inbound(lead=lead, agent=agent, text="Show me villas", now=NOW)

    assert result.escalated is True
    assert "threshold" in (result.escalation_reason or "")
    assert lead.status is LeadStatus.HANDED_OFF


async def test_model_requested_escalation_is_reported(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    llm = FakeLLM(
        tool_turn(
            ("escalate_to_human", {"reason": "Asked to speak to a person", "urgency": "normal"})
        ),
        text_turn("Of course — Rohan will call you shortly."),
    )
    engine = ConversationEngine(session, llm)

    result = await engine.handle_inbound(
        lead=lead, agent=agent, text="Can I talk to a real person?", now=NOW
    )

    assert result.escalated is True
    assert result.escalation_reason == "Asked to speak to a person"
    assert lead.status is LeadStatus.HANDED_OFF


async def test_engine_stays_quiet_once_a_human_has_taken_over(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    llm = FakeLLM(
        tool_turn(("escalate_to_human", {"reason": "price negotiation", "urgency": "high"})),
        text_turn("Rohan will follow up."),
        text_turn("this must not be sent"),
    )
    engine = ConversationEngine(session, llm)
    await engine.handle_inbound(lead=lead, agent=agent, text="What's your best price?", now=NOW)
    calls_before = len(llm.calls)

    result = await engine.handle_inbound(lead=lead, agent=agent, text="Hello?", now=NOW)

    assert len(llm.calls) == calls_before  # model not consulted again
    assert result.reply == ""
    assert result.needs_human is True


async def test_tool_failure_is_returned_to_the_model_not_raised(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    llm = FakeLLM(
        tool_turn(
            (
                "book_appointment",
                {
                    "starts_at": "not a date",
                    "appointment_type": "call",
                    "listing_id": None,
                    "notes": None,
                },
            )
        ),
        text_turn("Sorry, let me check the calendar again."),
    )
    engine = ConversationEngine(session, llm)

    result = await engine.handle_inbound(lead=lead, agent=agent, text="Book me in", now=NOW)

    assert result.error is None
    tool_result = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "ISO-8601" in tool_result["content"]
    assert result.reply.startswith("Sorry")


async def test_provider_failure_falls_back_and_escalates(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    engine = ConversationEngine(session, ExplodingLLM(LLMError("provider down")))

    result = await engine.handle_inbound(lead=lead, agent=agent, text="Hi", now=NOW)

    assert result.error == "provider down"
    assert result.escalated is True
    assert agent.name in result.reply
    assert lead.status is LeadStatus.HANDED_OFF


async def test_model_refusal_falls_back_and_escalates(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    engine = ConversationEngine(session, FakeLLM(refusal_turn()))

    result = await engine.handle_inbound(lead=lead, agent=agent, text="Hi", now=NOW)

    assert result.escalated is True
    assert lead.status is LeadStatus.HANDED_OFF
    assert result.reply


async def test_history_alternates_and_starts_with_the_lead(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    llm = FakeLLM(text_turn("One"), text_turn("Two"))
    engine = ConversationEngine(session, llm)

    await engine.handle_inbound(lead=lead, agent=agent, text="First", now=NOW)
    await engine.handle_inbound(lead=lead, agent=agent, text="Second", now=NOW)

    history = llm.calls[-1]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    assert history[0]["content"] == "First"
    assert history[-1]["content"] == "Second"


async def test_conversation_is_reused_across_turns(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    engine = ConversationEngine(session, FakeLLM(text_turn("a"), text_turn("b")))

    first = await engine.handle_inbound(lead=lead, agent=agent, text="1", now=NOW)
    second = await engine.handle_inbound(lead=lead, agent=agent, text="2", now=NOW)

    assert first.conversation.id == second.conversation.id


async def test_system_prompt_carries_agent_context_and_no_raw_phone(
    session: AsyncSession,
) -> None:
    agent, lead = await setup(session)
    lead.budget_max = Decimal("9000000")
    llm = FakeLLM(text_turn("hello"))
    engine = ConversationEngine(session, llm)

    await engine.handle_inbound(lead=lead, agent=agent, text="Hi", now=NOW)

    system = llm.last_system
    assert agent.name in system
    assert (agent.brokerage_name or "") in system
    assert "₹9,000,000" in system
    assert "1 active listing" in system
    assert agent.phone not in system  # masked


async def test_tool_loop_is_bounded(session: AsyncSession) -> None:
    agent, lead = await setup(session)
    empty = {
        **dict.fromkeys(
            ["listing_id", "city", "locality", "property_type", "bhk", "budget_min", "budget_max"]
        )
    }
    # More tool turns than max_tool_iterations allows.
    llm = FakeLLM(*[tool_turn(("get_listing_details", empty)) for _ in range(20)])
    engine = ConversationEngine(session, llm)

    result = await engine.handle_inbound(lead=lead, agent=agent, text="Hi", now=NOW)

    assert len(llm.calls) <= engine._settings.max_tool_iterations  # noqa: SLF001
    assert result.reply  # fell back rather than hanging
