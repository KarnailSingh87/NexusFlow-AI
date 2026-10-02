"""API schemas for document ingestion."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExtractionSummary(BaseModel):
    """What text extraction found, without shipping the text back."""

    model_config = ConfigDict(extra="forbid")

    characters: int = Field(description="Length of the extracted text.")
    pages: int = Field(description="Pages detected, or 1 for formats without pages.")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Format-specific extraction facts."
    )


class DocumentIngestResponse(BaseModel):
    """Result of a successful upload."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    filename: str
    format: str = Field(description="Detected format key: pdf, docx, txt, or csv.")
    status: str = Field(description="Pipeline status; 'ready' means vectors are stored.")
    size_bytes: int
    checksum_sha256: str
    page_count: int
    chunk_count: int
    token_count: int
    embedding_model: str | None = None
    embedded: bool = Field(default=False, description="Whether every chunk received a vector.")
    scan_engine: str = Field(description="Which malware screening layer ran.")
    extraction: ExtractionSummary
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal issues, e.g. pages with no text or skipped embedding.",
    )
    created_at: datetime
    processed_at: datetime | None = None


class DocumentSummary(BaseModel):
    """Document metadata without its chunks."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None
    checksum_sha256: str | None = None
    status: str
    page_count: int | None = None
    chunk_count: int = 0
    token_count: int = 0
    embedding_model: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    processed_at: datetime | None = None


class DocumentChunkOut(BaseModel):
    """A stored chunk with its raw text and stored vector."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    ordinal: int
    content: str
    page_number: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    token_count: int = 0
    embedding: list[float] | None = Field(
        default=None, description="Stored vector, when embeddings are enabled."
    )
    embedding_dimensions: int | None = Field(
        default=None, description="Length of the vector, if present."
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentDetail(DocumentSummary):
    """A document plus its chunks."""

    chunks: list[DocumentChunkOut] = Field(default_factory=list)


class DocumentListResponse(BaseModel):
    """Paginated list of documents."""

    model_config = ConfigDict(extra="forbid")

    items: list[DocumentSummary]
    total: int
