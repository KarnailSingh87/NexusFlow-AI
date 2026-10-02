"""Tests for settings loading and validation."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from app.core.config import Settings
from pydantic import ValidationError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Consumed by ``docker-entrypoint.sh``, not by ``Settings``.
_COMPOSE_ONLY_VARS = frozenset({"RUN_MIGRATIONS", "DB_WAIT_ATTEMPTS"})

#: Runtime knobs that silently stop working if Compose stops forwarding them.
#: The container receives *only* the explicit ``environment:`` list -- there is no
#: ``env_file`` and no ``.env`` mount -- so a variable missing from that block is
#: ignored even when it is set in ``.env``, with no error anywhere.
_MUST_REACH_CONTAINER = (
    "LOG_FORMAT",
    "DB_POOL_RECYCLE_SECONDS",
    "DB_POOL_PRE_PING",
    "DB_CONNECT_RETRIES",
    "DB_CONNECT_BACKOFF_SECONDS",
    "DB_FAIL_FAST",
)


def _backend_compose_environment() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml", reason="PyYAML is not a declared test dependency")
    compose = REPO_ROOT / "docker-compose.yml"
    if not compose.is_file():
        pytest.skip("docker-compose.yml not present in this checkout")
    return yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]["backend"]["environment"]


def test_compose_forwards_only_real_settings() -> None:
    """Every variable Compose injects must be a setting the app actually reads."""
    names = {name.lower() for name in Settings.model_fields}
    names |= {f.alias.lower() for f in Settings.model_fields.values() if f.alias}

    unknown = [
        key
        for key in _backend_compose_environment()
        if key not in _COMPOSE_ONLY_VARS and key.lower() not in names
    ]

    assert unknown == [], f"Compose injects variables Settings does not read: {unknown}"


@pytest.mark.parametrize("variable", _MUST_REACH_CONTAINER)
def test_runtime_settings_are_forwarded_to_the_container(variable: str) -> None:
    """Guard against the "set it in .env, Compose drops it" failure mode."""
    assert variable in _backend_compose_environment()


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


@pytest.mark.parametrize(
    ("variable", "value", "field", "expected"),
    [
        (
            "CORS_ORIGINS",
            "http://a.test,http://b.test",
            "cors_origins",
            ["http://a.test", "http://b.test"],
        ),
        ("CORS_ORIGINS", '["http://c.test"]', "cors_origins", ["http://c.test"]),
        ("CORS_ORIGINS", "*", "cors_origins", ["*"]),
        ("MODEL_ALLOWLIST", "nvidia/a,nvidia/b", "model_allowlist", ["nvidia/a", "nvidia/b"]),
        ("MODEL_ALLOWLIST", '["nvidia/c"]', "model_allowlist", ["nvidia/c"]),
    ],
)
def test_list_settings_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    field: str,
    expected: list[str],
) -> None:
    """CSV/JSON list settings must survive the *environment* source.

    Passing these as constructor kwargs bypasses ``EnvSettingsSource`` entirely,
    so kwargs alone never proved that ``CORS_ORIGINS=http://a,http://b`` — the form
    shipped in ``.env.example`` and ``docker-compose.yml`` — can boot the app.
    Without ``NoDecode`` pydantic-settings JSON-decodes the raw value and raises
    ``SettingsError`` before the CSV validator is ever reached.
    """
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("NEBIUS_API_KEY", "nb_test")
    monkeypatch.setenv(variable, value)

    assert getattr(Settings(), field) == expected


@pytest.mark.parametrize("variable", ["CORS_ORIGINS", "MODEL_ALLOWLIST"])
def test_empty_list_setting_means_explicitly_none(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """``KEY=`` is the shipped default and must mean "empty", not "unset".

    Falling back to the built-in default would silently re-open the documented
    localhost origins on a deployment that deliberately cleared them, so an empty
    value parses to ``[]`` and leaves the field's own default untouched.
    """
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("NEBIUS_API_KEY", "nb_test")
    monkeypatch.setenv(variable, "")

    assert getattr(Settings(), variable.lower()) == []


def test_shipped_env_example_loads_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every documented variable must be loadable exactly as documented."""
    import pathlib

    examples = [
        pathlib.Path(__file__).resolve().parents[2] / ".env.example",
        pathlib.Path(__file__).resolve().parents[1] / ".env.example",
    ]
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("NEBIUS_API_KEY", "nb_test")

    for example in examples:
        if not example.is_file():
            continue
        for raw in example.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            monkeypatch.setenv(key.strip(), value.strip())
        monkeypatch.delenv("APP_ENV")
        monkeypatch.delenv("NEBIUS_API_KEY")
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("NEBIUS_API_KEY", "nb_test")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.cors_origins == ["http://localhost:3000", "http://127.0.0.1:3000"]
        assert settings.model_allowlist == []


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


# ---------------------------------------------------------------------------
# Document ingestion settings
# ---------------------------------------------------------------------------
class TestIngestionSettings:
    def test_allowed_extensions_accepts_csv(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            UPLOAD_ALLOWED_EXTENSIONS="pdf,docx,txt,csv"
        )
        assert settings.upload_allowed_extensions == ["pdf", "docx", "txt", "csv"]

    def test_allowed_extensions_empty_means_all_supported(self) -> None:
        settings = Settings(UPLOAD_ALLOWED_EXTENSIONS="")  # type: ignore[call-arg]
        assert settings.upload_allowed_extensions == []

    def test_allowed_extensions_accepts_json(self) -> None:
        settings = Settings(UPLOAD_ALLOWED_EXTENSIONS='["pdf","csv"]')  # type: ignore[call-arg]
        assert settings.upload_allowed_extensions == ["pdf", "csv"]

    def test_upload_defaults_are_sane(self) -> None:
        settings = Settings()  # type: ignore[call-arg]
        assert settings.upload_max_bytes > 0
        assert settings.upload_storage_dir
        assert settings.ingest_chunk_size > 0
        assert settings.ingest_max_chunks > 0

    def test_chunk_overlap_defaults_below_chunk_size(self) -> None:
        """Overlap must not consume the whole chunk budget."""
        settings = Settings()  # type: ignore[call-arg]
        assert settings.ingest_chunk_overlap < settings.ingest_chunk_size

    def test_clamav_disabled_by_default(self) -> None:
        settings = Settings()  # type: ignore[call-arg]
        assert settings.clamav_host == ""
        assert settings.clamav_port == 3310

    def test_compose_forwards_every_ingestion_setting(self) -> None:
        compose = _backend_compose_environment()
        for key in (
            "UPLOAD_MAX_BYTES",
            "UPLOAD_STORAGE_DIR",
            "UPLOAD_ALLOWED_EXTENSIONS",
            "UPLOAD_REQUIRE_TEXT",
            "INGEST_CHUNK_SIZE",
            "INGEST_CHUNK_OVERLAP",
            "INGEST_MAX_CHUNKS",
            "INGEST_EMBED_CHUNKS",
            "CLAMAV_HOST",
            "CLAMAV_PORT",
            "DEV_AUTH_ENABLED",
            "DEV_USER_EMAIL",
        ):
            assert key in compose, f"{key} not forwarded by docker-compose"

    def test_compose_storage_dir_is_not_ephemeral_tmp(self) -> None:
        """Compose must not point uploads at a container-local temp dir."""
        assert _backend_compose_environment()["UPLOAD_STORAGE_DIR"] != ""

    def test_dev_auth_defaults_are_development_only(self) -> None:
        settings = Settings()  # type: ignore[call-arg]
        assert settings.dev_auth_enabled is True
        assert "@" in settings.dev_user_email
