"""Meta WhatsApp Cloud API webhooks.

Two endpoints, per Meta's contract:

* `GET`  — one-time subscription handshake; echo `hub.challenge` back verbatim.
* `POST` — message and status deliveries, authenticated by an HMAC over the raw body.

The POST handler acknowledges as soon as the message is safely recorded, then
puts it on the inbound queue for a worker to answer. Meta re-delivers anything it
does not see acknowledged within a few seconds, and a model turn takes longer
than that — so replying 200 first is what stops a slow turn from becoming a
duplicate reply. Because that 200 is final, whatever we do next has to be durable
on its own: hence the queue rather than an in-process task (M8).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp_payload import (
    SIGNATURE_HEADER,
    ParsedWebhook,
    parse_webhook,
    verify_signature,
)
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.logging import get_logger
from app.core.redis import get_redis, redis_available
from app.services.inbound_queue import enqueue, ensure_group
from app.services.ingestion import (
    Claim,
    UnknownAgentError,
    apply_status_updates,
    claim_inbound,
)
from app.services.turn import run_claim

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

    # Commit before dispatching: the worker (or the fallback task) opens its own
    # session and must be able to see the claimed rows.
    await session.commit()

    queued = await _dispatch(claims, background, settings)

    return {"accepted": len(claims), "queued": queued, "statuses_applied": applied}


async def _dispatch(claims: list[Claim], background: BackgroundTasks, settings: Settings) -> int:
    """Hand each claim to the inbound worker, or run it here if that is not possible.

    The queue is the durable path: the entry survives this process dying, and the
    worker retries it. Falling back to an in-process task when Redis is down is a
    deliberate downgrade to the pre-M8 behaviour — that path can lose a turn on a
    crash, but the alternative is losing it immediately and with certainty, since
    Meta treats our 200 as final and will not redeliver.
    """
    if not claims:
        return 0

    if settings.inbound_queue_enabled:
        client = get_redis(settings)
        if await redis_available(client):
            await ensure_group(client)
            enqueued = 0
            for claim in claims:
                try:
                    await enqueue(client, claim)
                    enqueued += 1
                except Exception:
                    log.exception("could not queue message %s; running it here", claim.message_id)
                    background.add_task(_process_in_process, claim)
            return enqueued

        log.error(
            "redis is unavailable; processing %s message(s) in-process. "
            "A crash before the reply will lose them.",
            len(claims),
        )

    for claim in claims:
        background.add_task(_process_in_process, claim)
    return 0


async def _process_in_process(claim: Claim) -> None:
    """Fallback path: run the turn inside the API process.

    Used when the queue is disabled or Redis is unreachable. In-process means a
    crash or redeploy between the ack and the reply loses that turn — which is
    exactly what the queue exists to prevent, so this should be rare and is
    logged as an error when it happens.
    """
    await run_claim(claim, get_settings())
