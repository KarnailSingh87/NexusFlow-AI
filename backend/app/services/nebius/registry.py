"""Curated NVIDIA Nemotron model registry served by NexusFlow.

This is a **local snapshot** of the Nemotron models available on Nebius Token
Factory, used to render a fast, offline-capable model picker in the UI. It is
deliberately not a source of truth for availability: ``GET /api/v1/models``
proxies the live ``/models`` endpoint, and anything listed here that Token
Factory no longer serves will simply fail at call time.

Keep this list in sync with the deprecation notices published at
https://docs.tokenfactory.nebius.com/deprecations
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ModelTier = Literal["fast", "balanced", "frontier", "embedding", "vision"]


@dataclass(frozen=True, slots=True)
class ModelCard:
    """Presentation metadata for one Nemotron model."""

    id: str
    label: str
    tier: ModelTier
    family: str
    description: str
    context_tokens: int
    max_output_tokens: int
    input_cost_per_mtok: float
    output_cost_per_mtok: float
    supports_tools: bool = True
    supports_vision: bool = False
    notes: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the card into a JSON-safe mapping."""
        return {
            "id": self.id,
            "label": self.label,
            "tier": self.tier,
            "family": self.family,
            "description": self.description,
            "context_tokens": self.context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "input_cost_per_mtok_usd": self.input_cost_per_mtok,
            "output_cost_per_mtok_usd": self.output_cost_per_mtok,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "notes": self.notes,
            "tags": list(self.tags),
        }


#: Prices are USD per 1M tokens, snapshot taken from the Token Factory pricing
#: page. Verify against the live console before relying on them for billing.
NEMOTRON_MODELS: tuple[ModelCard, ...] = (
    ModelCard(
        id="nvidia/Nemotron-3_5-Lightning",
        label="Nemotron 3.5 Lightning",
        tier="fast",
        family="nemotron-3",
        description=(
            "30B hybrid MoE (3B active) tuned for agentic reasoning, tool use and "
            "coding. The default for interactive workflows."
        ),
        context_tokens=1_048_576,
        max_output_tokens=32_768,
        input_cost_per_mtok=0.06,
        output_cost_per_mtok=0.24,
        tags=("recommended", "low-latency", "long-context"),
    ),
    ModelCard(
        id="nvidia/nemotron-3-super-120b-a12b",
        label="Nemotron 3 Super",
        tier="balanced",
        family="nemotron-3",
        description=(
            "120B hybrid MoE (12B active) for multi-agent orchestration and "
            "harder reasoning at moderate latency."
        ),
        context_tokens=262_144,
        max_output_tokens=32_768,
        input_cost_per_mtok=0.30,
        output_cost_per_mtok=0.90,
        tags=("multi-agent", "reasoning"),
    ),
    ModelCard(
        id="nvidia/Nemotron-3-Ultra-550b-a55b",
        label="Nemotron 3 Ultra",
        tier="frontier",
        family="nemotron-3",
        description=(
            "550B hybrid MoE (55B active) frontier model for deep research and "
            "long-running autonomous agents."
        ),
        context_tokens=1_048_576,
        max_output_tokens=32_768,
        input_cost_per_mtok=1.00,
        output_cost_per_mtok=3.00,
        tags=("frontier", "deep-research"),
    ),
    ModelCard(
        id="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        label="Nemotron 3 Nano",
        tier="fast",
        family="nemotron-3",
        description=(
            "Compact 30B MoE with strong multilingual support; a solid drop-in "
            "when Lightning is unavailable in a region."
        ),
        context_tokens=262_144,
        max_output_tokens=32_768,
        input_cost_per_mtok=0.06,
        output_cost_per_mtok=0.24,
        tags=("multilingual", "low-cost"),
    ),
    ModelCard(
        id="nvidia/Nemotron-Nano-V2-12b",
        label="Nemotron Nano V2 (Vision)",
        tier="vision",
        family="nemotron-nano",
        description="12B vision-language model for document and image understanding.",
        context_tokens=131_072,
        max_output_tokens=32_768,
        input_cost_per_mtok=0.06,
        output_cost_per_mtok=0.24,
        supports_vision=True,
        tags=("vision", "ocr"),
    ),
    ModelCard(
        id="Qwen/Qwen3-Embedding-8B",
        label="Qwen3 Embedding 8B",
        tier="embedding",
        family="qwen3",
        description="Text embedding model powering NexusFlow retrieval pipelines.",
        context_tokens=41_000,
        max_output_tokens=0,
        input_cost_per_mtok=0.01,
        output_cost_per_mtok=0.0,
        supports_tools=False,
        tags=("embeddings", "rag"),
    ),
)

#: Models withdrawn from Token Factory serverless on 2026-08-31. Kept so the
#: API can return an actionable error instead of a bare 404.
DEPRECATED_MODELS: dict[str, str] = {
    "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1": "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/Nemotron-3-Nano-Omni": "nvidia/Nemotron-3_5-Lightning",
}


def list_curated_models() -> list[dict[str, Any]]:
    """Return the curated registry as plain dicts."""
    return [card.to_dict() for card in NEMOTRON_MODELS]


def get_model_card(model_id: str) -> ModelCard | None:
    """Look up a curated card by exact model ID."""
    for card in NEMOTRON_MODELS:
        if card.id == model_id:
            return card
    return None


def is_nemotron_model(model_id: str) -> bool:
    """Return True when the ID belongs to the NVIDIA Nemotron family."""
    lowered = model_id.lower()
    return "nemotron" in lowered or lowered.startswith("nvidia/")
