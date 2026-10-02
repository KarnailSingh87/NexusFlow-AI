"""Document ingestion endpoints.

``POST /documents/upload`` accepts a single multipart file. Mounted under
``/api/v1`` to match the rest of the surface; the ingestion logic lives in
``app/services/documents/`` and is independent of HTTP.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import ConfigDep, CurrentUser, DbSession
from app.core.logging import get_logger
from app.db.models import Document, DocumentChunk, DocumentStatus
from app.schemas.documents import (
    DocumentChunkOut,
    DocumentDetail,
    DocumentIngestResponse,
    DocumentListResponse,
    ExtractionSummary,
)
from app.services.documents.chunking import ChunkingError
from app.services.documents.extractors import ExtractionError
from app.services.documents.pipeline import (
    IngestionError,
    IngestionResult,
    _remove_tree,
    ingest_document,
    make_nebius_embedder,
)
from app.services.documents.scanning import MalwareDetectedError
from app.services.documents.validation import (
    DocumentTooLargeError,
    UploadRejectedError,
)

router = APIRouter(prefix="/documents", tags=["documents"])
logger = get_logger(__name__)


def _ingest_error(exc: Exception) -> HTTPException:
    """Map an ingestion failure onto the right HTTP status.

    Status codes come from the exception classes themselves, so the mapping
    lives next to the failure it describes rather than in a catch-all here.
    """
    code = getattr(exc, "status_code", status.HTTP_422_UNPROCESSABLE_CONTENT)
    return HTTPException(status_code=code, detail=str(exc))


@router.post(
    "/upload",
    response_model=DocumentIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and ingest a document",
)
async def upload_document(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    config: ConfigDep,
    file: Annotated[UploadFile, File(description="PDF, DOCX, TXT, or CSV file.")],
) -> DocumentIngestResponse:
    """Validate, screen, parse, chunk, embed, and store one document.

    The file's real type is decided by its magic bytes, not by the declared
    content type or the extension. A file that fails validation or malware
    screening leaves nothing behind on disk.
    """
    client = getattr(request.app.state, "nebius_client", None)
    embedder = make_nebius_embedder(client) if client is not None else None

    try:
        result = await ingest_document(
            stream=file,
            filename=file.filename or "upload",
            declared_mime=file.content_type or "",
            owner_id=user.id,
            session=session,
            embedder=embedder,
            config=config,
        )
    except (
        DocumentTooLargeError,
        UploadRejectedError,
        MalwareDetectedError,
        ExtractionError,
        ChunkingError,
        IngestionError,
    ) as exc:
        logger.info("Rejected upload %r: %s", file.filename, exc)
        raise _ingest_error(exc) from exc

    created = await session.get(Document, result.document_id)
    return _to_ingest_response(result, created_at=created.created_at if created else None)


@router.get("", response_model=DocumentListResponse, summary="List your documents")
async def list_documents(
    session: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> DocumentListResponse:
    """Return the caller's documents, newest first."""
    total = await session.scalar(
        select(func.count()).select_from(Document).where(Document.owner_id == user.id)
    )
    rows = await session.scalars(
        select(Document)
        .where(Document.owner_id == user.id)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return DocumentListResponse(items=[_to_summary(row) for row in rows], total=total or 0)


@router.get("/{document_id}", response_model=DocumentDetail, summary="Get a document")
async def get_document(
    document_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> DocumentDetail:
    """Return one document with all of its chunks."""
    document = await _load_owned(session, user.id, document_id)
    detail = _to_summary(document)
    detail.chunks = [_to_chunk(chunk) for chunk in document.chunks]
    return detail


@router.get(
    "/{document_id}/chunks",
    response_model=list[DocumentChunkOut],
    summary="List a document's chunks",
)
async def list_chunks(
    document_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> list[DocumentChunkOut]:
    """Return the stored chunks, including their vectors when present."""
    document = await _load_owned(session, user.id, document_id)
    return [_to_chunk(chunk) for chunk in sorted(document.chunks, key=lambda c: c.ordinal)]


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
)
async def delete_document(document_id: uuid.UUID, session: DbSession, user: CurrentUser) -> None:
    """Delete a document, its chunks, and the stored file."""
    document = await _load_owned(session, user.id, document_id)
    stored = Path(document.storage_path)
    await session.delete(document)
    await session.commit()
    # Remove bytes only after the row is committed, so a failed delete leaves a
    # recoverable record rather than an orphaned blob.
    await asyncio.to_thread(_remove_tree, stored)


async def _load_owned(
    session: AsyncSession, owner_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """Fetch a document, enforcing ownership.

    A document belonging to someone else is reported as 404, not 403: revealing
    that an id exists is itself a leak.
    """
    document = await session.scalar(
        select(Document)
        .options(selectinload(Document.chunks))
        .where(Document.id == document_id, Document.owner_id == owner_id)
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return document


def _to_summary(document: Document) -> DocumentDetail:
    return DocumentDetail(
        id=document.id,
        filename=document.filename,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        checksum_sha256=document.checksum_sha256,
        status=document.status,
        page_count=document.page_count,
        chunk_count=document.chunk_count,
        token_count=document.token_count,
        embedding_model=document.embedding_model,
        error=document.error,
        metadata=dict(document.metadata_ or {}),
        created_at=document.created_at,
        updated_at=document.updated_at,
        processed_at=document.processed_at,
    )


def _to_chunk(chunk: DocumentChunk) -> DocumentChunkOut:
    return DocumentChunkOut(
        id=chunk.id,
        ordinal=chunk.ordinal,
        content=chunk.content,
        page_number=chunk.page_number,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        token_count=chunk.token_count,
        embedding=chunk.embedding,
        embedding_dimensions=len(chunk.embedding) if chunk.embedding else None,
        metadata=dict(chunk.metadata_ or {}),
    )


def _to_ingest_response(
    result: IngestionResult, *, created_at: datetime | None
) -> DocumentIngestResponse:
    """Shape an :class:`IngestionResult` for the API."""
    return DocumentIngestResponse(
        id=result.document_id,
        filename=result.filename,
        format=result.format,
        status=result.status.value,
        size_bytes=result.size_bytes,
        checksum_sha256=result.checksum_sha256,
        page_count=result.page_count,
        chunk_count=result.chunk_count,
        token_count=result.token_count,
        embedding_model=result.embedding_model,
        embedded=result.status is DocumentStatus.READY,
        scan_engine=result.scan_engine,
        extraction=ExtractionSummary(
            characters=result.extracted.characters,
            pages=result.extracted.pages,
            metadata=result.extracted.metadata,
        ),
        warnings=result.warnings,
        created_at=created_at or datetime.now(UTC),
        processed_at=None,
    )
