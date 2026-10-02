"""Application configuration.

All runtime settings are sourced from environment variables (optionally loaded
from a ``.env`` file). Nothing secret is ever hard-coded — see ``.env.example``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholders that must never reach production.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "change-me",
        "changeme",
        "change-me-in-production",
        "your-secret-key-here",
        "secret",
        "dev-secret-key-change-me",
        "please-change-me",
    }
)


class Settings(BaseSettings):
    """Typed, validated application settings."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_name: str = Field(default="NexusFlow AI API", alias="APP_NAME")
    app_slug: str = Field(default="nexusflow-backend", alias="APP_SLUG")
    app_env: Literal["development", "staging", "test", "production"] = Field(
        default="development", alias="APP_ENV"
    )
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    debug: bool = Field(default=False, alias="DEBUG")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    api_v1_prefix: str = Field(default="/api/v1", alias="API_V1_PREFIX")
    docs_enabled: bool = Field(default=True, alias="DOCS_ENABLED")

    # ------------------------------------------------------------------
    # Secrets / auth
    # ------------------------------------------------------------------
    secret_key: SecretStr = Field(default=SecretStr("dev-secret-key-change-me"), alias="SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(default=30, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    refresh_token_expire_days: int = Field(default=7, alias="REFRESH_TOKEN_EXPIRE_DAYS")

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: SecretStr | None = Field(default=None, alias="DATABASE_URL")
    postgres_user: str = Field(default="nexusflow", alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(default=SecretStr("nexusflow"), alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="nexusflow", alias="POSTGRES_DB")
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, alias="DB_MAX_OVERFLOW")
    db_echo: bool = Field(default=False, alias="DB_ECHO")

    # ------------------------------------------------------------------
    # Nebius Token Factory
    # ------------------------------------------------------------------
    nebius_api_key: SecretStr = Field(default=SecretStr(""), alias="NEBIUS_API_KEY")
    nebius_base_url: str = Field(
        default="https://api.tokenfactory.nebius.com/v1", alias="NEBIUS_BASE_URL"
    )
    nebius_timeout_seconds: float = Field(default=120.0, alias="NEBIUS_TIMEOUT_SECONDS")
    nebius_max_retries: int = Field(default=3, alias="NEBIUS_MAX_RETRIES")
    nebius_verify_tls: bool = Field(default=True, alias="NEBIUS_VERIFY_TLS")

    # ------------------------------------------------------------------
    # NVIDIA Nemotron model defaults
    # ------------------------------------------------------------------
    nemotron_fast_model: str = Field(
        default="nvidia/Nemotron-3_5-Lightning", alias="NEMOTRON_FAST_MODEL"
    )
    nemotron_balanced_model: str = Field(
        default="nvidia/nemotron-3-super-120b-a12b", alias="NEMOTRON_BALANCED_MODEL"
    )
    nemotron_frontier_model: str = Field(
        default="nvidia/Nemotron-3-Ultra-550b-a55b", alias="NEMOTRON_FRONTIER_MODEL"
    )
    nemotron_default_model: str | None = Field(default=None, alias="NEMOTRON_DEFAULT_MODEL")
    nemotron_embedding_model: str = Field(
        default="Qwen/Qwen3-Embedding-8B", alias="NEMOTRON_EMBEDDING_MODEL"
    )
    # Allow-lists keep arbitrary client-supplied model IDs from reaching the
    # upstream provider by accident.
    model_allowlist: list[str] = Field(default_factory=list, alias="MODEL_ALLOWLIST")

    # ------------------------------------------------------------------
    # Generation defaults
    # ------------------------------------------------------------------
    llm_default_temperature: float = Field(default=0.6, alias="LLM_DEFAULT_TEMPERATURE")
    llm_default_max_tokens: int = Field(default=2048, alias="LLM_DEFAULT_MAX_TOKENS")
    llm_max_max_tokens: int = Field(default=32768, alias="LLM_MAX_MAX_TOKENS")
    llm_max_input_tokens: int = Field(default=100_000, alias="LLM_MAX_INPUT_TOKENS")

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        alias="CORS_ORIGINS",
    )

    # ------------------------------------------------------------------
    # Optional infrastructure
    # ------------------------------------------------------------------
    redis_url: SecretStr | None = Field(default=None, alias="REDIS_URL")

    # ------------------------------------------------------------------
    # Bootstrap admin (first-run convenience only)
    # ------------------------------------------------------------------
    bootstrap_admin_email: str = Field(
        default="admin@nexusflow.local", alias="BOOTSTRAP_ADMIN_EMAIL"
    )
    bootstrap_admin_password: SecretStr | None = Field(
        default=None, alias="BOOTSTRAP_ADMIN_PASSWORD"
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("cors_origins", "model_allowlist", mode="before")
    @classmethod
    def _split_csv_or_json(cls, value: Any) -> Any:
        """Accept both JSON arrays and plain comma-separated lists."""
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("nebius_base_url")
    @classmethod
    def _normalise_base_url(cls, value: str) -> str:
        """Nebius expects an OpenAI-compatible base URL ending in ``/v1``."""
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            return "https://api.tokenfactory.nebius.com/v1"
        if not cleaned.endswith("/v1"):
            cleaned = f"{cleaned}/v1"
        return cleaned

    @field_validator("nebius_max_retries")
    @classmethod
    def _bound_retries(cls, value: int) -> int:
        return max(0, min(value, 10))

    @model_validator(mode="after")
    def _finalise(self) -> Settings:
        """Derive values and enforce production safety rails."""
        if not self.database_url:
            user = self.postgres_user
            password = self.postgres_password.get_secret_value()
            self.database_url = SecretStr(
                f"postgresql+asyncpg://{user}:{password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )

        if not self.nemotron_default_model:
            self.nemotron_default_model = self.nemotron_balanced_model

        if self.app_env == "production":
            problems: list[str] = []
            secret = self.secret_key.get_secret_value().strip()
            if secret.lower() in _PLACEHOLDER_SECRETS:
                problems.append("SECRET_KEY is still a placeholder")
            elif len(secret) < 32:
                problems.append("SECRET_KEY must be at least 32 characters")
            if not self.nebius_api_key.get_secret_value().strip():
                problems.append("NEBIUS_API_KEY is required")
            if "*" in self.cors_origins:
                problems.append("CORS_ORIGINS must not be '*' in production")
            if problems:
                raise ValueError("Invalid production configuration: " + "; ".join(problems))
        return self

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    @property
    def sqlalchemy_dsn(self) -> str:
        """Return the SQLAlchemy DSN with the password revealed."""
        assert self.database_url is not None  # guaranteed by _finalise
        return self.database_url.get_secret_value()

    @property
    def async_database_dsn(self) -> str:
        """Return a DSN guaranteed to use the ``asyncpg`` driver."""
        dsn = self.sqlalchemy_dsn
        if dsn.startswith("postgresql://"):
            return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        if dsn.startswith("postgres://"):
            return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
        return dsn

    @property
    def nebius_chat_url(self) -> str:
        """Return the fully-qualified chat completions URL."""
        return f"{self.nebius_base_url}/chat/completions"

    @property
    def nebius_models_url(self) -> str:
        """Return the fully-qualified model listing URL."""
        return f"{self.nebius_base_url}/models"

    @property
    def nebius_embeddings_url(self) -> str:
        """Return the fully-qualified embeddings URL."""
        return f"{self.nebius_base_url}/embeddings"

    @property
    def is_production(self) -> bool:
        """True when running with ``APP_ENV=production``."""
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings (usable as a FastAPI dependency)."""
    return Settings()


settings = get_settings()
