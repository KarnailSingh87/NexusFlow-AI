"""Chat / completion request and response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings


class ChatMessage(BaseModel):
    """A single OpenAI-style chat message.

    ``content`` is either a plain string or a list of content parts, which is
    what vision-capable Nemotron checkpoints expect.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: str | list[dict[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None

    @field_validator("content")
    @classmethod
    def _content_not_empty(cls, value: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if isinstance(value, str) and not value.strip():
            raise ValueError("message content must not be empty")
        return value


class ChatCompletionRequest(BaseModel):
    """Payload accepted by ``POST /api/v1/chat/completions``."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str | None = Field(
        default=None,
        description="Nemotron model ID. Defaults to NEMOTRON_DEFAULT_MODEL.",
        examples=["nvidia/Nemotron-3_5-Lightning"],
    )
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    stop: list[str] | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    response_format: dict[str, Any] | None = None
    user: str | None = Field(default=None, description="Opaque end-user identifier.")
    workflow_id: str | None = Field(
        default=None, description="Optional workflow to attribute this run to."
    )

    @model_validator(mode="after")
    def _approx_token_guard(self) -> ChatCompletionRequest:
        """Cheap guard rail: reject obviously oversized prompts before billing."""
        chars = sum(len(m.content) for m in self.messages if isinstance(m.content, str))
        approx_tokens = chars // 4
        limit = settings.llm_max_input_tokens
        if approx_tokens > limit:
            raise ValueError(
                f"Prompt is approximately {approx_tokens} tokens, which exceeds "
                f"LLM_MAX_INPUT_TOKENS ({limit})."
            )
        return self


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    """Normalised completion response (a subset of the OpenAI schema)."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: TokenUsage = Field(default_factory=TokenUsage)
    provider: str = "nebius-token-factory"
    latency_ms: int | None = None


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    input: list[str] = Field(min_length=1, description="Texts to embed.")
    model: str | None = Field(default=None, description="Defaults to NEMOTRON_EMBEDDING_MODEL.")


class EmbeddingResponse(BaseModel):
    model: str
    embeddings: list[list[float]]
    prompt_tokens: int = 0
