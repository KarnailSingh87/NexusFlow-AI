"""Model discovery endpoints.

``GET /models`` prefers the live Token Factory catalogue and enriches it with
NexusFlow's curated Nemotron cards. If the provider is unreachable the curated
catalogue is served instead, so the UI always has something to render.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import NebiusClientDep
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.models import ModelCatalogResponse, ModelInfo, ModelListResponse
from app.services.nebius import (
    DEPRECATED_MODELS,
    NebiusError,
    get_model_card,
    is_nemotron_model,
    list_curated_models,
)

router = APIRouter(prefix="/models", tags=["models"])
logger = get_logger(__name__)


def _enrich(model_id: str, raw: dict[str, Any] | None = None) -> ModelInfo:
    """Merge curated metadata over the raw upstream payload."""
    card = get_model_card(model_id)
    raw = raw or {}
    deprecated = model_id in DEPRECATED_MODELS
    return ModelInfo(
        id=model_id,
        label=card.label if card else None,
        tier=card.tier if card else None,
        family=card.family if card else None,
        description=card.description if card else None,
        context_tokens=card.context_tokens if card else None,
        max_output_tokens=card.max_output_tokens if card else None,
        input_cost_per_mtok_usd=card.input_cost_per_mtok if card else None,
        output_cost_per_mtok_usd=card.output_cost_per_mtok if card else None,
        supports_tools=card.supports_tools if card else None,
        supports_vision=card.supports_vision if card else None,
        owned_by=raw.get("owned_by"),
        created=raw.get("created"),
        deprecated=deprecated,
        replacement=DEPRECATED_MODELS.get(model_id),
        curated=card is not None,
    )


@router.get(
    "",
    response_model=ModelListResponse,
    summary="List available models",
)
async def list_models(
    nebius: NebiusClientDep,
    provider: str = Query(
        default="auto",
        pattern="^(auto|live|curated)$",
        description=(
            "`live` proxies Token Factory, `curated` uses the bundled registry, "
            "`auto` (default) prefers live and falls back to curated."
        ),
    ),
    nemotron_only: bool = Query(
        default=True, description="Restrict results to the NVIDIA Nemotron family."
    ),
) -> ModelListResponse:
    """Enumerate models available to the configured Token Factory project."""
    default_model = settings.nemotron_default_model or settings.nemotron_balanced_model
    source = "curated"
    enriched: list[ModelInfo] = []

    if provider in ("auto", "live"):
        try:
            upstream = await nebius.list_models()
        except NebiusError as exc:
            logger.warning("live model discovery failed (%s); using curated list", exc.message)
            if provider == "live":
                raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        else:
            source = "live"
            for entry in upstream:
                model_id = str(entry.get("id") or "").strip()
                if not model_id:
                    continue
                if nemotron_only and not is_nemotron_model(model_id):
                    continue
                enriched.append(_enrich(model_id, entry))

    if source == "curated":
        enriched = [_enrich(card["id"]) for card in list_curated_models()]

    # Default model first, then curated-tier models, then the rest.
    order = {"fast": 0, "balanced": 1, "frontier": 2, "vision": 3, "embedding": 4}
    enriched.sort(key=lambda m: (m.id != default_model, order.get(m.tier or "", 9), m.id))

    return ModelListResponse(
        default_model=default_model,
        source=source,
        data=enriched,
    )


@router.get(
    "/catalog",
    response_model=ModelCatalogResponse,
    summary="Curated Nemotron catalogue (no provider call)",
)
async def model_catalog() -> ModelCatalogResponse:
    """Serve static Nemotron cards with context windows and snapshot pricing."""
    return ModelCatalogResponse(source="curated", data=list_curated_models())


@router.get(
    "/{model_id:path}",
    response_model=ModelInfo,
    summary="Describe a single model",
)
async def get_model(model_id: str) -> ModelInfo:
    """Look up one model in the curated registry, flagging known deprecations."""
    normalised = model_id.strip().removeprefix("models/")
    if normalised not in DEPRECATED_MODELS and get_model_card(normalised) is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"'{normalised}' is not in the curated catalogue. "
                "Query GET /models for the live Token Factory inventory."
            ),
        )
    return _enrich(normalised)
