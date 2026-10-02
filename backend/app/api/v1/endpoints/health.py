"""Liveness and readiness probes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import NebiusClientDep
from app.core.config import settings
from app.db.session import check_database
from app.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Cheap check used by orchestrators; never touches the database."""
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        environment=settings.app_env,
        checks={"service": "up"},
    )


@router.get("/health/live", response_model=HealthResponse, include_in_schema=False)
async def liveness() -> HealthResponse:
    """Return 200 as long as the process can serve traffic."""
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        environment=settings.app_env,
        checks={"service": "up"},
    )


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    summary="Readiness probe",
    responses={503: {"description": "One or more dependencies are unavailable."}},
)
async def readiness(response: Response, nebius: NebiusClientDep) -> HealthResponse:
    """Verify that PostgreSQL and Nebius Token Factory are both reachable."""
    checks: dict[str, Any] = {}
    healthy = True

    db_ok = await check_database()
    checks["database"] = "up" if db_ok else "down"
    healthy = healthy and db_ok

    nebius_ok = await nebius.ping()
    checks["nebius_token_factory"] = "up" if nebius_ok else "down"
    # The API can still serve cached catalog data without the provider, so a
    # Nebius outage degrades rather than fails readiness.
    overall = "ok" if healthy else ("degraded" if not nebius_ok else "unavailable")

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall,
        version=settings.app_version,
        environment=settings.app_env,
        checks=checks,
    )
