"""Job handlers: the work each job type actually performs.

A handler receives a claimed :class:`BackgroundJob`, the telemetry recorder for
its attempt, and a progress callback. It returns a JSON-safe result dict that is
stored on the job row and returned verbatim by ``GET /jobs/{id}/result``.

Handlers must stay cooperative: ``asyncio.wait_for`` cannot preempt a handler
that never awaits, so the timeout is only a real bound because every handler
either awaits I/O or runs CPU-bound work in a thread.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import TokenUsage
from app.db.models import BackgroundJob, Document, DocumentChunk, DocumentStatus
from app.services.ai_service import AIService
from app.services.documents import chunking
from app.services.documents.extractors import (
    EmptyDocumentError,
    ExtractionError,
    extract_async,
)
from app.services.documents.pipeline import make_nebius_embedder
from app.services.jobs.telemetry import JobTelemetry

logger = get_logger(__name__)

#: Progress callback: ``await progress(percent, message, **detail)``.
ProgressFn = Callable[..., Awaitable[None]]


class JobCancelledError(Exception):
    """Raised at a checkpoint when the job has been asked to stop."""


class JobHandlerError(Exception):
    """A job failed for a reason worth retrying or surfacing verbatim."""

    def __init__(self, message: str, *, code: str = "job_failed") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def run_document_extraction(
    job: BackgroundJob,
    session: AsyncSession,
    telemetry: JobTelemetry,
    progress: ProgressFn,
    *,
    nebius_client: Any | None = None,
    config: Any | None = None,
) -> dict[str, Any]:
    """Re-extract an already-uploaded document from its stored bytes.

    This is the heavy half of ingestion: parsing a 500-page audit and building
    chunks is CPU-bound and takes far longer than a request may. Doing it here
    keeps the upload endpoint fast and lets the caller poll instead of wait.
    """
    document_id = job.document_id or _uuid_from_payload(job.payload, "document_id")
    if document_id is None:
        raise JobHandlerError("No document_id on the job or in its payload.", code="no_document")

    document = await session.get(Document, document_id)
    if document is None:
        raise JobHandlerError(f"Document {document_id} not found.", code="document_missing")

    stored = Path(document.storage_path)
    # exists() stats the filesystem, so it runs in a thread like the rest of the
    # blocking path work.
    if not await asyncio.to_thread(stored.exists):
        raise JobHandlerError(
            f"Stored bytes for document {document_id} are missing at {stored}.",
            code="source_missing",
        )

    await progress(5, "Re-parsing stored document", filename=document.filename)

    metadata = dict(document.metadata_ or {})
    fmt = str(metadata.get("format") or _format_from_path(stored))
    # Parsing is CPU-bound, so it runs in a thread; awaiting it inline would
    # stall this worker, the HTTP event loop, and every other job with it.
    try:
        extracted = await extract_async(stored, fmt)
    except EmptyDocumentError as exc:
        raise JobHandlerError(str(exc), code="empty_document") from exc
    except ExtractionError as exc:
        # Encrypted PDFs, corrupt OOXML packages, and unreadable encodings all
        # land here. A specific code lets a client tell "retry this later" from
        # "this file will never parse", which is the difference between a useful
        # failure and a retry loop that burns the budget on a bad file.
        raise JobHandlerError(str(exc), code="extraction_failed") from exc

    if not extracted.text.strip():
        raise JobHandlerError("Document produced no extractable text.", code="empty_document")

    await progress(45, "Extracted text", characters=len(extracted.text), pages=extracted.page_count)

    chunks = await asyncio.to_thread(
        chunking.chunk_document,
        extracted,
        chunk_size=getattr(config, "ingest_chunk_size", 1200),
        overlap=getattr(config, "ingest_chunk_overlap", 200),
        max_chunks=getattr(config, "ingest_max_chunks", 2000),
    )
    if not chunks:
        raise JobHandlerError("Chunking produced no chunks.", code="empty_chunks")

    await progress(60, "Chunked", chunks=len(chunks))

    # Replace any previous attempt's chunks. This job is the authority on the
    # derived data, and stale vectors from an earlier run would poison retrieval.
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))

    embedder = make_nebius_embedder(nebius_client) if nebius_client is not None else None
    vectors: list[list[float]] = []
    if embedder is not None:
        vectors = await embedder([chunking.chunk_to_embedding_input(chunk) for chunk in chunks])
        if len(vectors) != len(chunks):
            raise JobHandlerError(
                f"Embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks.",
                code="embedding_mismatch",
            )
    embedded = len(vectors)

    total_tokens = sum(chunk.token_count for chunk in chunks)
    total = len(chunks)
    for index, chunk in enumerate(chunks):
        session.add(
            DocumentChunk(
                document_id=document_id,
                ordinal=index,
                content=chunk.content,
                page_number=chunk.page_number,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                token_count=chunk.token_count,
                embedding=vectors[index] if embedded else None,
                metadata_=chunk.metadata,
            )
        )
        if (index + 1) % 25 == 0 or index == total - 1:
            await session.flush()
            await progress(
                60 + int(35 * (index + 1) / total),
                "Storing chunks",
                stored=index + 1,
                total=total,
            )

    document.page_count = extracted.page_count
    document.chunk_count = total
    document.token_count = total_tokens
    document.status = DocumentStatus.READY if embedded == total else DocumentStatus.EMBEDDING
    document.processed_at = datetime.now(tz=UTC)
    await session.flush()

    await progress(100, "Stored", chunks=total, embedded=embedded)
    return {
        "document_id": str(document_id),
        "filename": document.filename,
        "characters": len(extracted.text),
        "pages": extracted.page_count,
        "chunk_count": total,
        "embedded_chunks": embedded,
        "estimated_tokens": total_tokens,
        "status": document.status.value,
        "extraction": extracted.metadata,
    }


async def run_embedding(
    job: BackgroundJob,
    session: AsyncSession,
    telemetry: JobTelemetry,
    progress: ProgressFn,
    *,
    nebius_client: Any | None = None,
    config: Any | None = None,
) -> dict[str, Any]:
    """Embed chunks that were stored as raw text because the provider was down."""
    if nebius_client is None:
        raise JobHandlerError(
            "No embedding client is configured; set NEBIUS_API_KEY.", code="no_client"
        )
    document_id = job.document_id or _uuid_from_payload(job.payload, "document_id")
    if document_id is None:
        raise JobHandlerError("No document_id on the job or in its payload.", code="no_document")

    rows = (
        await session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.ordinal.asc())
        )
    ).all()
    if not rows:
        raise JobHandlerError(f"Document {document_id} has no chunks.", code="no_chunks")

    pending = [chunk for chunk in rows if not chunk.embedding]
    if not pending:
        await progress(100, "Nothing to embed", already_embedded=len(rows))
        return {
            "document_id": str(document_id),
            "embedded_chunks": 0,
            "already_embedded": len(rows),
        }

    embedder = make_nebius_embedder(nebius_client)
    await progress(10, "Embedding pending chunks", pending=len(pending))

    vectors = await embedder([chunk.content for chunk in pending])
    if len(vectors) != len(pending):
        raise JobHandlerError(
            f"Embedding provider returned {len(vectors)} vectors for {len(pending)} chunks.",
            code="embedding_mismatch",
        )
    for index, (chunk, vector) in enumerate(zip(pending, vectors, strict=True)):
        chunk.embedding = list(vector)
        if (index + 1) % 25 == 0 or index == len(pending) - 1:
            await session.flush()
            await progress(
                10 + int(80 * (index + 1) / len(pending)),
                "Embedding",
                embedded=index + 1,
                total=len(pending),
            )

    document = await session.get(Document, document_id)
    if document is not None:
        document.status = DocumentStatus.READY
    await progress(100, "Embedded", embedded=len(pending))
    return {
        "document_id": str(document_id),
        "embedded_chunks": len(pending),
        "already_embedded": len(rows) - len(pending),
    }


async def run_llm_task(
    job: BackgroundJob,
    session: AsyncSession,
    telemetry: JobTelemetry,
    progress: ProgressFn,
    *,
    nebius_client: Any | None = None,
    config: Any | None = None,
) -> dict[str, Any]:
    """Run a routed Nemotron completion and report its real token usage.

    This is the job type that produces provider-reported token counts rather
    than an estimate, so it is what proves the telemetry wiring end to end.
    """
    if nebius_client is None:
        raise JobHandlerError(
            "No Nebius client is configured; set NEBIUS_API_KEY.", code="no_client"
        )
    payload = dict(job.payload or {})
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise JobHandlerError("Payload must include a non-empty 'prompt'.", code="no_prompt")

    task_type = str(payload.get("task_type") or "chat")
    await progress(10, "Routing to Nemotron", task_type=task_type)

    service = AIService(client=nebius_client, task_type=task_type, config=config)
    started = time.perf_counter()
    document, result = await service.chat_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=payload.get("max_tokens"),
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    usage = result.usage
    telemetry.record_tokens(
        TokenUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_tokens=usage.cached_tokens,
        ),
        model=result.model,
    )
    await progress(90, "Completion received", model=result.model, latency_ms=latency_ms)
    return {
        "model": result.model,
        "task_type": str(result.task_type),
        "tier": result.tier,
        "latency_ms": latency_ms,
        "usage": usage.as_dict(),
        "output": document if isinstance(document, str) else result.content,
    }


#: Job type → handler. Adding a workload means adding an entry here; nothing
#: else in the queue needs to change.
HANDLERS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "document_extraction": run_document_extraction,
    "document_embedding": run_embedding,
    "embedding": run_embedding,
    "custom": run_llm_task,
    "workflow_run": run_llm_task,
}


def _uuid_from_payload(payload: dict[str, Any], key: str) -> uuid.UUID | None:
    raw = (payload or {}).get(key)
    if raw in (None, ""):
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _format_from_path(path: Path) -> str:
    """Best-effort format guess from a stored file's extension.

    Only used when the document row has no recorded format. The stored extension
    came from the *detected* format at upload time, so this is a fallback rather
    than a trust decision.
    """
    return path.suffix.lstrip(".").lower() or "txt"


__all__ = [
    "HANDLERS",
    "JobCancelledError",
    "JobHandlerError",
    "run_document_extraction",
    "run_embedding",
    "run_llm_task",
]
