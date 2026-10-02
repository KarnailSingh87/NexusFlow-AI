"""End-to-end document ingestion: bytes in, chunked records out.

The pipeline is deliberately written as a sequence of small, individually
testable stages so a failure always has one obvious place to happen:

``stage`` → ``validate`` → ``screen`` → ``promote`` → ``extract`` → ``chunk`` →
``embed`` → ``persist``

Two behaviours matter more than the happy path:

* **Cleanup is unconditional.** If any stage raises, the staged file, the
  promoted file, and the ``Document`` row are removed. A failed upload must not
  leave attacker-controlled bytes sitting in the storage volume.
* **Embedding is best-effort.** Chunks are always persisted with their raw text.
  If the embedding provider is down, ingestion still succeeds and the document
  is left in ``embedding`` status with the reason recorded, ready for a retry.
  Losing the vectors is recoverable; losing the whole document is not.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.logging import get_logger
from app.db.models import Document, DocumentChunk, DocumentStatus
from app.services.documents import scanning, validation
from app.services.documents.chunking import (
    TextChunk,
    chunk_document,
    chunk_to_embedding_input,
)
from app.services.documents.extractors import extract_async

logger = get_logger(__name__)

#: Streamed read size for both the size check and the checksum.
_READ_BLOCK: Final = 1024 * 1024
#: Embedding models accept far fewer inputs per request than this in practice;
#: batching keeps payloads within provider limits.
_EMBED_BATCH: Final = 64

#: Signature of a callable that turns texts into vectors.
Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class UploadStream(Protocol):
    """Async byte source. Satisfied by Starlette's ``UploadFile`` directly."""

    async def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes, or fewer at end of stream."""
        ...


class EmbedderProtocol(Protocol):
    """The slice of :class:`NebiusClient` that ingestion depends on."""

    async def create_embeddings(
        self, *, input_texts: Iterable[str], model: str | None = None
    ) -> list[list[float]]:
        """Turn texts into vectors using the configured embedding model."""
        ...


class IngestionError(RuntimeError):
    """Base class for ingestion failures that are not request-level errors."""

    status_code = 500


class UnsupportedDocumentError(IngestionError):
    """Raised when a document cannot be ingested at all."""

    status_code = 422


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Outcome of a successful ingestion."""

    document_id: uuid.UUID
    filename: str
    format: str
    size_bytes: int
    checksum_sha256: str
    page_count: int
    chunk_count: int
    token_count: int
    status: DocumentStatus
    storage_path: str
    embedding_model: str | None
    scan_engine: str
    extracted: ExtractedTextSummary
    warnings: list[str]

    @property
    def embedded(self) -> bool:
        """Whether vectors were stored for every chunk."""
        return self.status is DocumentStatus.READY


@dataclass(frozen=True, slots=True)
class ExtractedTextSummary:
    """Small, JSON-safe view of what extraction found."""

    characters: int
    pages: int
    metadata: dict[str, object]


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------
async def stage_upload(
    stream: UploadStream,
    *,
    config: Settings | None = None,
    directory: Path | None = None,
) -> Path:
    """Stream an upload to a temp file, enforcing the size limit as it arrives.

    The limit is checked per block rather than after buffering, so a 2 GB body
    is refused after one block instead of after 2 GB of disk writes.
    """
    cfg = config or settings
    target_dir = directory or Path(cfg.upload_storage_dir) / ".staging"
    target_dir.mkdir(parents=True, exist_ok=True)

    handle = await asyncio.to_thread(
        tempfile.mkstemp, dir=str(target_dir), prefix="upload-", suffix=".part"
    )
    fd, raw_path = handle
    path = Path(raw_path)

    total = 0
    try:
        with os.fdopen(fd, "wb") as sink:
            while block := await stream.read(_READ_BLOCK):
                total += len(block)
                if total > cfg.upload_max_bytes:
                    raise validation.DocumentTooLargeError(
                        f"Upload exceeds the {cfg.upload_max_bytes}-byte limit."
                    )
                sink.write(block)
    except BaseException:
        # Any failure, including cancellation, must not leave the partial file.
        await asyncio.to_thread(_remove_tree, path)
        raise

    if total == 0:
        await asyncio.to_thread(_remove_tree, path)
        raise validation.UploadRejectedError("Uploaded file is empty.")
    return path


def checksum_of(path: Path) -> str:
    """SHA-256 of a file, streamed so large documents do not enter memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_READ_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def promote(
    path: Path,
    *,
    config: Settings | None = None,
    doc_id: uuid.UUID,
    detected_format: str,
) -> Path:
    """Move a validated file into its final, UUID-named home.

    The stored name is generated by the server. The client-supplied filename is
    never used as a path component, so traversal and collisions are impossible.

    The extension comes from the *detected* format rather than from the staged
    file (which is always ``.part``) or from the client. That keeps the stored
    artifact self-describing without ever trusting a client-supplied suffix.
    """
    cfg = config or settings
    spec = validation.FORMATS.get(detected_format)
    if spec is None:  # pragma: no cover - validate() rejects these first
        raise validation.UploadRejectedError(f"Unknown detected format: {detected_format!r}.")
    root = Path(cfg.upload_storage_dir) / str(doc_id)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{doc_id}.{spec.extension}"
    shutil.move(str(path), str(destination))
    return destination


def read_head(path: Path, size: int = validation.SNIFF_BYTES) -> bytes:
    """Read the leading bytes used for format sniffing."""
    with path.open("rb") as handle:
        return handle.read(size)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
async def ingest_document(
    *,
    stream: UploadStream,
    filename: str,
    declared_mime: str,
    owner_id: uuid.UUID,
    session: AsyncSession,
    embedder: Embedder | None = None,
    config: Settings | None = None,
) -> IngestionResult:
    """Run a single upload all the way to persisted, embedded chunks.

    Args:
        stream: Async byte source, typically the multipart upload.
        filename: Client-supplied filename; sanitised, never used as a path.
        declared_mime: Client-supplied Content-Type; advisory only.
        owner_id: User the document belongs to.
        session: Open database session; this function commits.
        embedder: Optional vectoriser. When omitted or failing, the document is
            stored without vectors rather than being rejected.
        config: Settings override, primarily for tests.
    """
    cfg = config or settings
    safe_name = validation.sanitise_filename(filename)
    warnings: list[str] = []

    staged: Path | None = None
    stored: Path | None = None

    try:
        # 1. Stage to disk with a hard size ceiling.
        staged = await stage_upload(stream, config=cfg)

        # 2. Decide the real type from the bytes, not from what the client said.
        detected = validation.detect_format(
            read_head(staged), filename=safe_name, declared=declared_mime
        )
        validation.validate_extension_matches_content(safe_name, detected, declared=declared_mime)
        validation.ensure_extension_allowed(safe_name, cfg.upload_allowed_extensions)
        validation.enforce_size_limit(staged.stat().st_size, cfg.upload_max_bytes)

        # 3. Screen for malware before anything parses the file.
        scan = await scanning.screen_file(staged, config=cfg)
        scanning.raise_if_infected(scan, filename=safe_name)
        if scan.detail:
            warnings.append(scan.detail)
        if scan.engine not in {"eicar", "eicar+clamav(unavailable)"}:
            logger.info("Screened %s with %s", safe_name, scan.engine)

        # 4. Move to permanent storage and record the document.
        doc_id = uuid.uuid4()
        stored = promote(staged, config=cfg, doc_id=doc_id, detected_format=detected)
        staged = None

        digest = checksum_of(stored)
        document = Document(
            id=doc_id,
            owner_id=owner_id,
            filename=safe_name,
            storage_path=str(stored),
            mime_type=declared_mime or None,
            size_bytes=stored.stat().st_size,
            checksum_sha256=digest,
            status=DocumentStatus.PROCESSING.value,
            metadata_={},
        )
        session.add(document)
        await session.flush()

        # 5. Extract text. A document with no text is a permanent failure.
        document.status = DocumentStatus.UPLOADED.value
        extracted = await extract_async(stored, detected)
        if not extracted.text.strip() and cfg.upload_require_text:
            raise UnsupportedDocumentError(f"{safe_name} produced no extractable text.")
        for key in ("pages_without_text", "pages_truncated"):
            if extracted.metadata.get(key):
                warnings.append(f"{key}={extracted.metadata[key]}")

        # 6. Chunk on semantic boundaries.
        chunks = chunk_document(
            extracted,
            chunk_size=cfg.ingest_chunk_size,
            overlap=cfg.ingest_chunk_overlap,
            max_chunks=cfg.ingest_max_chunks,
        )
        if chunks and chunks[-1].metadata.get("truncated"):
            warnings.append(f"chunk cap {cfg.ingest_max_chunks} reached; tail dropped")

        # 7. Embed. Failure degrades the document, it does not reject it.
        embeddings = await _embed_chunks(chunks, embedder, cfg, warnings)

        # 8. Persist chunks and finalise the document.
        for chunk in chunks:
            session.add(
                DocumentChunk(
                    document_id=doc_id,
                    ordinal=chunk.ordinal,
                    content=chunk.content,
                    page_number=chunk.page_number,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    token_count=chunk.token_count,
                    embedding=embeddings.get(chunk.ordinal),
                    metadata_=chunk.metadata,
                )
            )

        document.status = (
            DocumentStatus.READY.value
            if embeddings or not cfg.ingest_embed_chunks
            else DocumentStatus.EMBEDDING.value
        )
        if cfg.ingest_embed_chunks and embeddings:
            document.embedding_model = cfg.nemotron_embedding_model
        document.page_count = extracted.page_count
        document.chunk_count = len(chunks)
        document.token_count = sum(chunk.token_count for chunk in chunks)
        document.metadata_ = {
            **extracted.metadata,
            "scan_engine": scan.engine,
            "scan_verdict": scan.verdict.value,
            "embedded_chunks": len(embeddings),
            "chunk_size": cfg.ingest_chunk_size,
            "chunk_overlap": cfg.ingest_chunk_overlap,
        }
        document.processed_at = document.processed_at or datetime.now(UTC)
        await session.commit()

        logger.info(
            "Ingested %s (%s): %d chunks, %d tokens, status=%s",
            safe_name,
            detected,
            len(chunks),
            document.token_count,
            document.status,
        )

        return IngestionResult(
            document_id=doc_id,
            filename=safe_name,
            format=detected,
            size_bytes=document.size_bytes or 0,
            checksum_sha256=digest,
            page_count=extracted.page_count,
            chunk_count=len(chunks),
            token_count=document.token_count,
            status=DocumentStatus(document.status),
            storage_path=str(stored),
            embedding_model=document.embedding_model,
            scan_engine=scan.engine,
            extracted=ExtractedTextSummary(
                characters=len(extracted.text),
                pages=extracted.page_count,
                metadata=dict(extracted.metadata),
            ),
            warnings=warnings,
        )

    except BaseException:
        # Both paths are cleaned: `staged` still holds the bytes when the
        # failure happened before promotion (validation, screening, parsing),
        # and leaving a rejected file behind would be exactly the leak this
        # pipeline must not have.
        await _rollback(session, staged=staged, stored_path=stored)
        raise


async def _embed_chunks(
    chunks: list[TextChunk],
    embedder: Embedder | None,
    cfg: Settings,
    warnings: list[str],
) -> dict[int, list[float]]:
    """Vectorise chunks, returning ``ordinal -> vector``.

    Returns an empty mapping on any provider failure. The caller stores the raw
    text and leaves the document re-embeddable.
    """
    if not cfg.ingest_embed_chunks:
        return {}
    if embedder is None:
        warnings.append("no embedder configured; chunks stored as raw text only")
        logger.warning("Embedding requested but no embedder is available")
        return {}
    if not chunks:
        return {}

    vectors: dict[int, list[float]] = {}
    try:
        for start in range(0, len(chunks), _EMBED_BATCH):
            batch = chunks[start : start + _EMBED_BATCH]
            texts = [chunk_to_embedding_input(chunk) for chunk in batch]
            result = await embedder(texts)
            if len(result) != len(batch):
                raise IngestionError(
                    f"Embedding provider returned {len(result)} vectors for {len(batch)} inputs."
                )
            vectors.update(
                {chunk.ordinal: vector for chunk, vector in zip(batch, result, strict=True)}
            )
    except Exception as exc:
        warnings.append(f"embedding skipped: {type(exc).__name__}")
        logger.warning("Embedding failed; storing raw chunks only: %s", exc)
        return {}
    return vectors


async def _rollback(
    session: AsyncSession, *, staged: Path | None, stored_path: Path | None
) -> None:
    """Undo a partial ingestion: the database row and every file both go."""
    try:
        await session.rollback()
    except Exception as exc:
        logger.warning("Rollback during ingestion cleanup failed: %s", exc)
    if staged is not None:
        # Blocking unlink in async code; keep it off the event loop like _remove_tree.
        await asyncio.to_thread(_remove_file, staged)
    if stored_path is not None:
        # Blocking unlink in an async cleanup path: hand it to a thread so a
        # slow or hung filesystem cannot stall the event loop.
        await asyncio.to_thread(_remove_tree, stored_path)


def _remove_file(path: Path) -> None:
    """Delete a single file, ignoring it if it is already gone."""
    with suppress(OSError):
        path.unlink()


def _remove_tree(path: Path) -> None:
    """Delete a stored file and its now-empty parent directory."""
    path.unlink(missing_ok=True)
    with suppress(OSError):
        path.parent.rmdir()


def make_nebius_embedder(client: EmbedderProtocol) -> Embedder:
    """Adapt a Nebius client into the ``Embedder`` callable the pipeline wants."""

    async def _embed(texts: list[str]) -> list[list[float]]:
        return await client.create_embeddings(input_texts=texts)

    return _embed
