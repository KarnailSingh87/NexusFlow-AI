"""Text extraction for each supported upload format.

Every extractor returns the same :class:`ExtractedText` shape so the chunker does
not care where text came from. Two details matter for downstream retrieval
quality:

* **Page numbers are preserved** where the format has pages (PDF), because
  citations that point at "page 4" are far more useful than bare offsets.
* **Character offsets are absolute across the whole document**, so a chunk can be
  traced back to the exact span of the original text.

Parsers are run in a worker thread: ``pypdf`` and ``lxml`` are synchronous and
CPU-bound, and parsing inline would stall the event loop for every concurrent
upload.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Parsers log a great deal of noise about malformed files we already reject.
logging.getLogger("pypdf").setLevel(logging.ERROR)

#: Refuse absurd CSV field widths; a missing quote can otherwise balloon memory.
_MAX_CSV_FIELD: Final = 1024 * 1024
#: BOM-keyed encodings. These are tried first because a UTF-16 file decoded as
#: cp1252 succeeds silently and yields mojibake, so the BOM must win outright.
_BOM_ENCODINGS: Final = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)
#: Encoding fallback order for BOM-less text. ``latin-1`` never fails and is
#: reached only after strict UTF-8 and cp1252 have both failed.
_FALLBACK_ENCODINGS: Final = ("utf-8", "cp1252", "latin-1")


class ExtractionError(ValueError):
    """Raised when a file's bytes cannot be turned into text."""

    status_code = 422


class EmptyDocumentError(ExtractionError):
    """Raised when extraction succeeds but yields no usable text."""

    status_code = 422


@dataclass(frozen=True, slots=True)
class Page:
    """One page or logical section of a document."""

    text: str
    number: int
    #: Absolute character offset of ``text`` within the document. Recorded at
    #: build time so offset-to-page mapping never has to re-search the text.
    start: int = 0


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """Full text of a document plus the page boundaries within it."""

    text: str
    pages: list[Page] = field(default_factory=list)
    #: Format-specific facts surfaced in the API and stored in document metadata.
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        """Number of pages, or ``1`` when the format has no page concept."""
        return len(self.pages) or 1

    def page_of(self, offset: int) -> int | None:
        """Return the page containing character ``offset``.

        Pages are contiguous in document order, so the answer is the last page
        whose start offset is at or before ``offset``.
        """
        if not self.pages:
            return None
        current = self.pages[0].number
        for page in self.pages:
            if page.start > offset:
                break
            current = page.number
        return current

    def with_metadata(self, extra: dict[str, object]) -> ExtractedText:
        """Return a copy carrying ``extra`` metadata (the dataclass is frozen)."""
        return ExtractedText(text=self.text, pages=self.pages, metadata={**self.metadata, **extra})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def extract(path: Path, fmt: str) -> ExtractedText:
    """Extract text from ``path`` using the parser for ``fmt``.

    Synchronous; call via :func:`extract_async` from async code.
    """
    handler = _EXTRACTORS.get(fmt)
    if handler is None:
        raise ExtractionError(f"No extractor registered for format {fmt!r}.")
    return handler(path)


async def extract_async(path: Path, fmt: str) -> ExtractedText:
    """Run :func:`extract` in a worker thread."""
    return await asyncio.to_thread(extract, path, fmt)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def extract_pdf(path: Path) -> ExtractedText:
    """Extract text from a PDF, keeping one :class:`Page` per PDF page.

    Pages with no text layer (scans without OCR) are skipped rather than
    producing empty chunks; :attr:`ExtractedText.metadata` records how many were
    skipped so the caller can warn that the document needs OCR.
    """
    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError, PdfReadError

    try:
        reader = PdfReader(str(path), strict=False)
        # Some producers emit infinite/looping page trees; cap the read.
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ExtractionError(
                    "PDF is password protected; supply an unlocked copy."
                ) from exc
        raw_pages = reader.pages[:_MAX_PDF_PAGES]
    except FileNotDecryptedError as exc:
        # Raised during construction when the trailer is encrypted, before the
        # is_encrypted branch below can offer an empty-password attempt.
        raise ExtractionError("PDF is password protected; supply an unlocked copy.") from exc
    except (PdfReadError, OSError, ValueError) as exc:
        raise ExtractionError(f"Could not read PDF: {exc}") from exc

    parts: list[str] = []
    empty_pages = 0

    for index, page in enumerate(raw_pages, start=1):
        try:
            content = (page.extract_text() or "").strip()
        except Exception as exc:
            logger.warning("Skipping unreadable page %d of %s: %s", index, path.name, exc)
            content = ""
        if not content:
            empty_pages += 1
            continue
        parts.append(content)

    assembled = _assemble(parts)
    if not assembled.text:
        raise EmptyDocumentError(
            "PDF contains no extractable text. Scanned documents need OCR first."
        )

    metadata: dict[str, object] = {"pages_total": len(reader.pages)}
    if empty_pages:
        metadata["pages_without_text"] = empty_pages
    if len(reader.pages) > _MAX_PDF_PAGES:
        metadata["pages_truncated"] = True

    return ExtractedText(text=assembled.text, pages=assembled.pages, metadata=metadata)


_MAX_PDF_PAGES: Final = 2000


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def extract_docx(path: Path) -> ExtractedText:
    """Extract text from a .docx, preserving paragraph breaks.

    Tables are flattened row-per-line with `` | `` separators rather than
    discarded; that keeps tabular content retrievable instead of silently lost.
    """
    import docx

    try:
        document = docx.Document(str(path))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # python-docx raises PackageNotFoundError for a file that only looks
        # like an OOXML package, and BadZipFile for a corrupt container. Both
        # mean "this is not a usable .docx", so they map to a client error.
        raise ExtractionError(f"Could not read DOCX: {exc}") from exc
    except Exception as exc:
        raise ExtractionError(f"Could not read DOCX: {exc}") from exc

    blocks: list[str] = []
    for paragraph in document.paragraphs:
        content = paragraph.text.strip()
        if content:
            blocks.append(content)

    table_rows = 0
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                blocks.append(" | ".join(cells))
                table_rows += 1

    assembled = _assemble(blocks)
    if not assembled.text:
        raise EmptyDocumentError("DOCX contains no extractable text.")

    metadata: dict[str, object] = {"tables": len(document.tables), "table_rows": table_rows}
    return assembled.with_metadata(metadata)


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------
def decode_text(raw: bytes) -> tuple[str, str]:
    """Decode bytes as text, returning ``(text, encoding_used)``.

    A BOM is honoured outright, then strict UTF-8, then the single-byte legacy
    encodings. Both orderings matter: ``latin-1`` decodes *any* byte sequence and
    ``cp1252`` happily decodes UTF-16 as mojibake, so trying them first would
    corrupt documents instead of reporting them.
    """
    for bom, encoding in _BOM_ENCODINGS:
        if raw.startswith(bom):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                break

    for encoding in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # Unreachable in practice: latin-1 cannot fail.
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def extract_txt(path: Path) -> ExtractedText:
    """Extract a plain-text file."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExtractionError(f"Could not read text file: {exc}") from exc

    text, encoding = decode_text(raw)
    stripped = text.strip()
    if not stripped:
        raise EmptyDocumentError("Text file is empty.")

    return _assemble([stripped]).with_metadata(
        {"encoding": encoding, "lines": stripped.count("\n") + 1}
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def extract_csv(path: Path) -> ExtractedText:
    """Extract CSV rows as one text block per row.

    Header detection matters for retrieval quality: a row that omits its
    header leaves the values meaningless, so the header is prepended to the first
    chunk and recorded in metadata. Delimiter is sniffed rather than assumed,
    since exports are frequently tab- or semicolon-separated despite the
    extension.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExtractionError(f"Could not read CSV: {exc}") from exc

    decoded, encoding = decode_text(raw)
    if not decoded.strip():
        raise EmptyDocumentError("CSV is empty.")

    try:
        dialect: type[csv.Dialect] = csv.Sniffer().sniff(decoded[:8192], delimiters=",;\t|")
    except csv.Error:
        # A single-column file has no delimiter for the sniffer to find.
        class _Default(csv.Dialect):
            delimiter = ","
            quotechar = '"'
            doublequote = True
            skipinitialspace = True
            lineterminator = "\r\n"
            quoting = csv.QUOTE_MINIMAL

        dialect = _Default  # type: ignore[assignment]

    # A malformed CSV missing a quote can otherwise grow a single field without
    # bound; cap it and treat the overflow as a parse failure. The limit is
    # process-global in the csv module, so it is restored afterwards.
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(min(_MAX_CSV_FIELD, max(previous_limit, 0)))
    try:
        rows = list(csv.reader(io.StringIO(decoded), dialect=dialect, strict=True))
    except (csv.Error, ValueError) as exc:
        raise ExtractionError(f"Malformed CSV: {exc}") from exc
    finally:
        csv.field_size_limit(previous_limit)

    cleaned = [row for row in rows if any(cell.strip() for cell in row)]
    if not cleaned:
        raise EmptyDocumentError("CSV has no rows with content.")

    header = cleaned[0]
    body = cleaned[1:] if len(cleaned) > 1 else []

    def render(row: list[str], index: int) -> str:
        pairs = [
            f"{name.strip()}: {value.strip()}"
            for name, value in zip(header, row, strict=False)
            if name.strip()
        ]
        lead = pairs or [", ".join(cell.strip() for cell in row)]
        return f"Row {index}: " + "; ".join(lead)

    lines = [render(row, index) for index, row in enumerate(body, start=1)]
    assembled = _assemble(lines)
    if not assembled.text:
        raise EmptyDocumentError("CSV has a header but no data rows.")

    return assembled.with_metadata(
        {
            "encoding": encoding,
            "delimiter": dialect.delimiter,
            "columns": [column.strip() for column in header],
            "rows": len(body),
        }
    )


def _join_blocks(blocks: list[str]) -> str:
    """Join logical blocks with a blank line, preserving order."""
    return "\n\n".join(block for block in blocks if block)


def _normalise_block(block: str) -> str:
    """Collapse horizontal whitespace runs and trim, keeping newlines.

    Applied once, at extraction time, so the document text handed to the chunker
    is already clean. Doing it here rather than mid-chunking is what lets the
    chunker report character offsets that index the stored text exactly.
    """
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in block.splitlines()]
    return "\n".join(line for line in lines if line)


def _assemble(parts: list[str], numbers: list[int] | None = None) -> ExtractedText:
    """Join blocks into one document body and build the offset index.

    Single source of truth for the document text *and* the page table, so page
    offsets can never drift from the text they describe.

    ``numbers`` is supplied only by formats that genuinely have pages (PDF).
    When it is ``None`` the format is unpaginated -- DOCX, TXT, and CSV have no
    page concept -- and the whole body becomes page 1. Synthesising a page per
    paragraph would report a three-line DOCX as a four-page document and point
    readers at pages that do not exist.
    """
    kept = [
        (_normalise_block(block), numbers[i] if numbers else 1)
        for i, block in enumerate(parts)
        if block
    ]
    kept = [(block, number) for block, number in kept if block]
    if not kept:
        return ExtractedText(text="", pages=[])

    if numbers is None:
        text = _join_blocks([block for block, _ in kept])
        return ExtractedText(text=text, pages=[Page(text=text, number=1, start=0)])

    text = _join_blocks([block for block, _ in kept])
    pages: list[Page] = []
    cursor = 0
    for block, number in kept:
        pages.append(Page(text=block, number=number, start=cursor))
        # +2 accounts for the blank-line separator _join_blocks inserts.
        cursor += len(block) + 2
    return ExtractedText(text=text, pages=pages)


_EXTRACTORS: Final[dict[str, Callable[[Path], ExtractedText]]] = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "txt": extract_txt,
    "csv": extract_csv,
}
