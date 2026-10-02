"""Service layer: external providers and domain logic.

``ai_service`` is the task-routing layer callers should reach for when they want
NexusFlow to decide *which* NVIDIA Nemotron model handles a request.
``nebius`` holds the lower-level transport client and the curated registry.
"""

from app.services.ai_service import (
    AIResult,
    AIService,
    StructuredOutputError,
    TaskRoute,
    UnknownTaskError,
    explain_routing,
    get_nemotron_client,
    resolve_task_route,
)
from app.services.nebius import (
    NebiusAuthError,
    NebiusClient,
    NebiusConfigurationError,
    NebiusError,
    NebiusNotFoundError,
    NebiusRateLimitError,
    NebiusUpstreamError,
)

__all__ = [
    "AIResult",
    "AIService",
    "NebiusAuthError",
    "NebiusClient",
    "NebiusConfigurationError",
    "NebiusError",
    "NebiusNotFoundError",
    "NebiusRateLimitError",
    "NebiusUpstreamError",
    "StructuredOutputError",
    "TaskRoute",
    "UnknownTaskError",
    "explain_routing",
    "get_nemotron_client",
    "resolve_task_route",
]
