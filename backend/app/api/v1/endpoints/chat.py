"""Chat completion endpoints backed by NVIDIA Nemotron on Nebius Token Factory."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse

from app.api.deps import NebiusClientDep
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    EmbeddingRequest,
    EmbeddingResponse,
    TokenUsage,
)
from app.services.nebius import NebiusError

router = APIRouter(prefix="/chat", tags=["chat"])
logger = get_logger(__name__)

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Disable proxy buffering so tokens appear as they are generated.
    "X-Accel-Buffering": "no",
}


def _normalise_completion(
    payload: dict[str, Any], *, model: str, latency_ms: int
) -> ChatCompletionResponse:
    """Map an OpenAI-shaped Token Factory body onto our response schema."""
    raw_usage = payload.get("usage") or {}
    usage = TokenUsage(
        prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
        completion_tokens=int(raw_usage.get("completion_tokens") or 0),
        total_tokens=int(raw_usage.get("total_tokens") or 0),
    )

    choices: list[ChatCompletionChoice] = []
    for index, choice in enumerate(payload.get("choices") or []):
        message = choice.get("message") or {}
        content = message.get("content")
        if content is None:
            content = ""
        choices.append(
            ChatCompletionChoice(
                index=int(choice.get("index", index)),
                message=ChatMessage(role="assistant", content=content),
                finish_reason=choice.get("finish_reason"),
            )
        )

    return ChatCompletionResponse(
        id=str(payload.get("id") or f"chatcmpl-{uuid.uuid4().hex}"),
        created=int(payload.get("created") or int(time.time())),
        model=str(payload.get("model") or model),
        choices=choices,
        usage=usage,
        latency_ms=latency_ms,
    )


@router.post(
    "/completions",
    response_model=ChatCompletionResponse,
    response_model_exclude_none=True,
    summary="Create a chat completion",
    responses={
        401: {"description": "Nebius rejected NEBIUS_API_KEY."},
        429: {"description": "Nebius rate limit or quota exceeded."},
        502: {"description": "Nebius upstream failure."},
    },
)
async def create_completion(
    payload: ChatCompletionRequest, nebius: NebiusClientDep
) -> ChatCompletionResponse:
    """Run a single non-streaming completion against a Nemotron model."""
    started = time.perf_counter()
    logger.info(
        "chat.completions model=%s messages=%d stream=%s",
        payload.model or settings.nemotron_default_model,
        len(payload.messages),
        payload.stream,
    )

    upstream = await nebius.create_chat_completion(
        messages=[m.model_dump(exclude_none=True) for m in payload.messages],
        model=payload.model,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        top_p=payload.top_p,
        stop=payload.stop,
        stream=False,
        tools=payload.tools,
        response_format=payload.response_format,
    )

    latency_ms = int((time.perf_counter() - started) * 1000)
    return _normalise_completion(
        upstream,
        model=payload.model or settings.nemotron_default_model or "",
        latency_ms=latency_ms,
    )


@router.post(
    "/completions/stream",
    summary="Stream a chat completion (SSE)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": ("OpenAI-compatible `data: {...}` frames terminated by `data: [DONE]`."),
        },
        401: {"description": "Nebius rejected NEBIUS_API_KEY."},
        429: {"description": "Nebius rate limit or quota exceeded."},
    },
)
async def stream_completion(
    payload: ChatCompletionRequest, nebius: NebiusClientDep
) -> StreamingResponse:
    """Proxy Token Factory's server-sent-events stream to the browser verbatim.

    Frames are forwarded unmodified so the Next.js client can consume the
    standard OpenAI streaming protocol. If the upstream connection fails
    mid-stream we emit a final ``nexusflow_error`` event, because the HTTP
    status is already committed by then.
    """

    async def event_source() -> AsyncIterator[str]:
        try:
            async for line in nebius.stream_chat_completion(
                messages=[m.model_dump(exclude_none=True) for m in payload.messages],
                model=payload.model,
                temperature=payload.temperature,
                max_tokens=payload.max_tokens,
                top_p=payload.top_p,
                stop=payload.stop,
                tools=payload.tools,
            ):
                if line.startswith("data:"):
                    yield f"{line}\n\n"
                else:
                    yield f"data: {line}\n\n"
        except NebiusError as exc:
            logger.warning("stream aborted: %s", exc.message)
            error_frame = json.dumps(
                {
                    "error": {
                        "code": exc.__class__.__name__,
                        "message": exc.message,
                    }
                }
            )
            yield f"data: {error_frame}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )


@router.post(
    "/embeddings",
    response_model=EmbeddingResponse,
    summary="Create embeddings",
)
async def create_embeddings(
    payload: EmbeddingRequest, nebius: NebiusClientDep
) -> EmbeddingResponse:
    """Embed one or more texts (defaults to the configured embedding model)."""
    vectors = await nebius.create_embeddings(input_texts=payload.input, model=payload.model)
    return EmbeddingResponse(
        model=payload.model or settings.nemotron_embedding_model,
        embeddings=vectors,
    )
