"""Aggregate router for API v1.

Health probes are intentionally **not** mounted here — they live at the app
root (``/health``) so orchestrators and load balancers can reach them without
knowing the API version prefix.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import chat, documents, models

api_router = APIRouter()
api_router.include_router(models.router)
api_router.include_router(chat.router)
api_router.include_router(documents.router)

__all__ = ["api_router"]
