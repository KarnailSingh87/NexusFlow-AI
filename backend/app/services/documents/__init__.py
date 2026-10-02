"""Document ingestion: validation, screening, extraction, chunking, persistence.

Import the specific module you need rather than the whole pipeline; the pipeline
pulls in the database layer, while ``validation`` and ``chunking`` are pure and
usable on their own.
"""

from app.services.documents.chunking import (
    ChunkingError,
    TextChunk,
    chunk_document,
    chunk_to_embedding_input,
    estimate_tokens,
)
from app.services.documents.extractors import (
    EmptyDocumentError,
    ExtractedText,
    ExtractionError,
    Page,
    decode_text,
    extract,
    extract_async,
)
from app.services.documents.scanning import (
    EICAR_SIGNATURE,
    ClamAvScanner,
    MalwareDetectedError,
    Scanner,
    ScanResult,
    ScanVerdict,
    raise_if_infected,
    screen_file,
)
from app.services.documents.validation import (
    FORMATS,
    DocumentFormat,
    DocumentTooLargeError,
    UploadRejectedError,
    detect_format,
    enforce_size_limit,
    ensure_extension_allowed,
    sanitise_filename,
    validate_extension_matches_content,
)

__all__ = [
    "EICAR_SIGNATURE",
    "FORMATS",
    "ChunkingError",
    "ClamAvScanner",
    "DocumentFormat",
    "DocumentTooLargeError",
    "EmptyDocumentError",
    "ExtractedText",
    "ExtractionError",
    "MalwareDetectedError",
    "Page",
    "ScanResult",
    "ScanVerdict",
    "Scanner",
    "TextChunk",
    "UploadRejectedError",
    "chunk_document",
    "chunk_to_embedding_input",
    "decode_text",
    "detect_format",
    "enforce_size_limit",
    "ensure_extension_allowed",
    "estimate_tokens",
    "extract",
    "extract_async",
    "raise_if_infected",
    "sanitise_filename",
    "screen_file",
    "validate_extension_matches_content",
]
