"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.nebius import NebiusClient, NebiusConfigurationError


async def get_nebius_client(request: Request) -> AsyncIterator[NebiusClient]:
    """Yield the long-lived Nebius client created during application startup.

    The client is constructed in ``lifespan`` so that its connection pool is
    shared across requests. If ``NEBIUS_API_KEY`` was missing at boot we still
    serve the rest of the API and fail loudly only when inference is requested.
    """
    client: NebiusClient | None = getattr(request.app.state, "nebius_client", None)
    if client is None:
        raise NebiusConfigurationError(
            "Nebius client is not initialised. Set NEBIUS_API_KEY in backend/.env "
            "and restart the API."
        )
    yield client


DbSession = Annotated[AsyncSession, Depends(get_session)]
NebiusClientDep = Annotated[NebiusClient, Depends(get_nebius_client)]
