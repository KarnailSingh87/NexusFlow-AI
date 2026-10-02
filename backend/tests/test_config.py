"""Tests for settings loading and validation."""

from __future__ import annotations

import pytest
from app.core.config import Settings
from pydantic import ValidationError


def test_database_url_is_derived_from_postgres_parts() -> None:
    settings = Settings(DATABASE_URL=None)  # type: ignore[call-arg]

    assert settings.sqlalchemy_dsn == (
        "postgresql+asyncpg://nexusflow:nexusflow@localhost:5432/nexusflow"
    )
    assert settings.async_database_dsn.startswith("postgresql+asyncpg://")


def test_explicit_database_url_is_preserved() -> None:
    settings = Settings(DATABASE_URL="postgresql://user:pw@db:5432/analytics")  # type: ignore[call-arg]

    # The plain postgresql:// scheme is upgraded to the async driver.
    assert settings.async_database_dsn == ("postgresql+asyncpg://user:pw@db:5432/analytics")


@pytest.mark.parametrize(
    "supplied",
    [
        "https://api.tokenfactory.nebius.com/v1",
        "https://api.tokenfactory.nebius.com/v1/",
        "https://api.tokenfactory.nebius.com",
    ],
)
def test_base_url_is_normalised_to_v1(supplied: str) -> None:
    settings = Settings(NEBIUS_BASE_URL=supplied)  # type: ignore[call-arg]

    assert settings.nebius_base_url == "https://api.tokenfactory.nebius.com/v1"
    assert settings.nebius_chat_url == ("https://api.tokenfactory.nebius.com/v1/chat/completions")


def test_cors_origins_accept_csv_and_json() -> None:
    csv = Settings(CORS_ORIGINS="http://a.test, http://b.test")  # type: ignore[call-arg]
    as_json = Settings(CORS_ORIGINS='["http://c.test"]')  # type: ignore[call-arg]

    assert csv.cors_origins == ["http://a.test", "http://b.test"]
    assert as_json.cors_origins == ["http://c.test"]


def test_default_model_falls_back_to_balanced() -> None:
    settings = Settings(NEMOTRON_DEFAULT_MODEL=None)  # type: ignore[call-arg]

    assert settings.nemotron_default_model == settings.nemotron_balanced_model


def test_production_rejects_placeholder_secret() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY is still a placeholder"):
        Settings(  # type: ignore[call-arg]
            APP_ENV="production",
            SECRET_KEY="change-me",
            NEBIUS_API_KEY="real-key",
        )


def test_production_rejects_missing_nebius_key() -> None:
    with pytest.raises(ValidationError, match="NEBIUS_API_KEY is required"):
        Settings(  # type: ignore[call-arg]
            APP_ENV="production",
            SECRET_KEY="k" * 48,
            NEBIUS_API_KEY="",
        )


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS must not be"):
        Settings(  # type: ignore[call-arg]
            APP_ENV="production",
            SECRET_KEY="k" * 48,
            NEBIUS_API_KEY="real-key",
            CORS_ORIGINS="*",
        )


def test_production_accepts_valid_configuration() -> None:
    settings = Settings(  # type: ignore[call-arg]
        APP_ENV="production",
        SECRET_KEY="k" * 48,
        NEBIUS_API_KEY="real-key",
        CORS_ORIGINS="https://app.nexusflow.example",
    )

    assert settings.is_production is True
