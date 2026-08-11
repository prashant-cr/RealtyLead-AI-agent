"""FastAPI routers."""

from fastapi import APIRouter

from app.api import accounts, dashboard, health, oauth, onboarding, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(accounts.router)
api_router.include_router(dashboard.router)
api_router.include_router(onboarding.router)
api_router.include_router(oauth.router)
api_router.include_router(webhooks.router)

__all__ = ["api_router"]
