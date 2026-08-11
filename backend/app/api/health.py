"""Liveness and readiness probes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.services.inbound_queue import depth, ensure_group

router = APIRouter(tags=["health"])
log = get_logger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]
    queue: dict[str, int] | None = None


@router.get("/health", response_model=HealthResponse)
async def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Liveness: the process is up. No dependencies touched."""
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
        version="0.1.0",
    )


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReadinessResponse:
    """Readiness: dependencies we cannot serve traffic without."""
    checks: dict[str, str] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        log.warning("readiness: database check failed: %s", type(exc).__name__)
        checks["database"] = "error"

    # Redis is reported but deliberately does not gate readiness. The webhook
    # falls back to in-process handling when it is down, so the API can still
    # serve traffic — pulling the instance out of the load balancer for this
    # would turn a degraded service into an unavailable one.
    queue: dict[str, int] | None = None
    try:
        client = get_redis()
        await ensure_group(client)
        queue = await depth(client)
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        log.warning("readiness: redis check failed: %s", type(exc).__name__)
        checks["redis"] = "degraded"

    ready = checks.get("database") == "ok"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if ready else "degraded", checks=checks, queue=queue)
