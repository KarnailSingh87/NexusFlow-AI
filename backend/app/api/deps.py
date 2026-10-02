"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import User
from app.db.session import get_session
from app.services.nebius import NebiusClient, NebiusConfigurationError

#: Sentinel stored as ``hashed_password`` for auto-provisioned dev accounts. Not
#: a valid hash, so password authentication can never succeed for them.
# S105: this is a deny-marker, not a credential; no password verifies
# against it because it is not a valid hash encoding.
NO_PASSWORD_LOGIN = "!no-password-login!"  # noqa: S105


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


def get_config() -> Settings:
    """Return application settings as an injectable dependency.

    Going through a dependency (rather than importing the module-level
    ``settings`` singleton) keeps configuration overridable in tests and lets a
    deployment inject a different instance without touching globals.
    """
    return get_settings()


async def get_current_user(request: Request, session: DbSession, config: ConfigDep) -> User:
    """Resolve the acting user.

    **This is not authentication.** While JWT verification is unimplemented, the
    principal is taken from the ``X-User-Email`` header (or a configured
    fallback) and the user row is auto-provisioned on first sight, so any client
    can claim any identity. That is deliberate and temporary: it exists so the
    ownership-scoped endpoints are exercisable before auth lands.

    Set ``DEV_AUTH_ENABLED=false`` to reject every request instead of trusting
    the header. Replace this dependency with real token verification before
    exposing the API beyond a trusted network.
    """
    if not config.dev_auth_enabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "No authentication backend is configured. DEV_AUTH_ENABLED is "
                "false and JWT verification is not implemented yet."
            ),
        )

    email = (request.headers.get("X-User-Email") or config.dev_user_email).strip().lower()
    if not email or "@" not in email or len(email) > 320:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-User-Email must be a valid email address.",
        )

    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        return existing

    user = User(
        email=email,
        full_name=email.split("@", 1)[0],
        # Placeholder for the NOT NULL column. Deliberately not a valid hash:
        # a dev-provisioned account must be incapable of password login, so no
        # password can ever be verified against this value.
        hashed_password=NO_PASSWORD_LOGIN,
        is_active=True,
    )
    session.add(user)
    try:
        await session.commit()
    except Exception:
        # Another request may have created the same user concurrently.
        await session.rollback()
        raced = await session.scalar(select(User).where(User.email == email))
        if raced is None:
            raise
        return raced
    await session.refresh(user)
    return user


DbSession = Annotated[AsyncSession, Depends(get_session)]
ConfigDep = Annotated[Settings, Depends(get_config)]
NebiusClientDep = Annotated[NebiusClient, Depends(get_nebius_client)]
CurrentUser = Annotated[User, Depends(get_current_user)]
