"""Meta WhatsApp Cloud API webhooks.

Two endpoints, per Meta's contract:

* `GET`  — one-time subscription handshake; echo `hub.challenge` back verbatim.
* `POST` — message and status deliveries, authenticated by an HMAC over the raw body.

The POST handler acknowledges as soon as the message is safely recorded and runs
the model turn in the background. Meta re-delivers anything it does not see
acknowledged within a few seconds, and a model turn takes longer than that — so
replying 200 first is what stops a slow turn from becoming a duplicate reply.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm import AnthropicLLM, LLMError
from app.channels.whatsapp import WhatsAppChannel, WhatsAppError
from app.channels.whatsapp_payload import (
    SIGNATURE_HEADER,
    ParsedWebhook,
    parse_webhook,
    verify_signature,
)
from app.core.config import Settings, get_settings
from app.core.db import get_session, get_sessionmaker
from app.core.logging import get_logger
from app.services.google_calendar import GoogleCalendarClient
from app.services.ingestion import (
    Claim,
    UnknownAgentError,
    apply_status_updates,
    claim_inbound,
    process_claimed,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger(__name__)


@router.get("/whatsapp", response_class=PlainTextResponse)
async def verify_subscription(
    settings: Annotated[Settings, Depends(get_settings)],
    hub_mode: Annotated[str | None, Header(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Header(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Header(alias="hub.challenge")] = None,
    *,
    request: Request,
) -> str:
    """Meta's subscription handshake. Params arrive as query string, not headers."""
    params = request.query_params
    mode = params.get("hub.mode", hub_mode)
    token = params.get("hub.verify_token", hub_verify_token)
    challenge = params.get("hub.challenge", hub_challenge)

    if not settings.whatsapp_verify_token:
        log.error("webhook verification attempted but WHATSAPP_VERIFY_TOKEN is not set")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "verification not configured")

    if mode != "subscribe" or token != settings.whatsapp_verify_token:
        log.warning("rejected webhook verification (mode=%s)", mode)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")

    log.info("whatsapp webhook verified")
    return challenge or ""


async def _authenticated_payload(
    request: Request, settings: Settings, signature: str | None
) -> ParsedWebhook:
    if not settings.whatsapp_app_secret:
        log.error("webhook received but WHATSAPP_APP_SECRET is not set")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webhook not configured")

    raw = await request.body()
    if not verify_signature(raw, signature, settings.whatsapp_app_secret):
        log.warning("rejected webhook delivery with a bad signature")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    try:
        payload: dict[str, Any] = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed JSON") from exc

    return parse_webhook(payload)


@router.post("/whatsapp", status_code=status.HTTP_200_OK)
async def receive(
    request: Request,
    background: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> dict[str, Any]:
    parsed = await _authenticated_payload(request, settings, signature)

    applied = await apply_status_updates(session, parsed.statuses)

    claims: list[Claim] = []
    for inbound in parsed.messages:
        try:
            claim = await claim_inbound(session, inbound)
        except UnknownAgentError as exc:
            # Acknowledge anyway: retrying will not make the agent appear, and an
            # error response makes Meta redeliver this batch indefinitely.
            log.warning("dropping delivery: %s", exc)
            continue
        if claim is not None:
            claims.append(claim)

    # Commit before scheduling: the background task opens its own session and must
    # be able to see the claimed rows.
    await session.commit()

    for claim in claims:
        background.add_task(_process_in_background, claim)

    return {"accepted": len(claims), "statuses_applied": applied}


async def _process_in_background(claim: Claim) -> None:
    """Run the model turn and send the reply, outside the request/response cycle.

    FastAPI background tasks are in-process: a crash or redeploy between the ack
    and the reply loses that turn. M5 replaces this with the Redis-backed worker
    already in the stack — see docs/decisions.md.
    """
    settings = get_settings()
    adapter: WhatsAppChannel | None = None
    calendar: GoogleCalendarClient | None = None
    try:
        llm = AnthropicLLM(settings)
    except LLMError as exc:
        log.error("cannot process claim %s: %s", claim.message_id, exc)
        return

    async with get_sessionmaker()() as session:
        try:
            from app.models import Agent

            agent = await session.get_one(Agent, claim.agent_id)
            if not agent.whatsapp_phone_number_id:
                log.error("agent %s has no whatsapp_phone_number_id", agent.id)
                return
            adapter = WhatsAppChannel(agent.whatsapp_phone_number_id, settings)
            if agent.google_refresh_token:
                calendar = GoogleCalendarClient(settings)
            await process_claimed(session, llm, adapter, claim, settings, calendar=calendar)
            await session.commit()
        except (WhatsAppError, LLMError) as exc:
            await session.rollback()
            log.error("failed to process claim %s: %s", claim.message_id, exc)
        except Exception:
            await session.rollback()
            log.exception("unexpected failure processing claim %s", claim.message_id)
        finally:
            if adapter is not None:
                await adapter.close()
            if calendar is not None:
                await calendar.close()
