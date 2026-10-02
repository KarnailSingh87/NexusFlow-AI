"""Model-discovery schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelInfo(BaseModel):
    """A model exposed to the UI.

    Merges live Token Factory metadata with NexusFlow's curated Nemotron card
    when one exists, so the picker can show context window and pricing without
    a round-trip to the provider.
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str
    label: str | None = None
    tier: str | None = None
    family: str | None = None
    description: str | None = None
    context_tokens: int | None = None
    max_output_tokens: int | None = None
    input_cost_per_mtok_usd: float | None = None
    output_cost_per_mtok_usd: float | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    owned_by: str | None = None
    created: int | None = None
    deprecated: bool = False
    replacement: str | None = None
    curated: bool = False


class ModelListResponse(BaseModel):
    object: str = "list"
    default_model: str
    source: str = Field(description="`live` (proxied) or `curated` (fallback).")
    data: list[ModelInfo]


class ModelCatalogResponse(BaseModel):
    object: str = "catalog"
    source: str = "curated"
    data: list[dict[str, Any]]
