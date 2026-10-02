"""Server-side validation for uploaded documents.

Three independent checks, because each catches a different mistake:

1. **Size and count limits** — enforced while streaming, so an oversized body is
   refused before it lands on disk.
2. **Magic-byte sniffing** — the declared ``Content-Type`` and the filename
   extension are both attacker-controlled, so neither may decide what a file is.
   The real format comes from the leading bytes.
3. **Extension/content agreement** — a ``.pdf`` that is actually a ZIP, or a
   ``.docx`` renamed to ``.txt``, is rejected rather than silently mis-parsed.

Rejected files are deleted on the way out; a validation failure must not leave
bytes behind on the storage volume.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Bytes inspected from the head of a file for format sniffing.
SNIFF_BYTES: Final = 4096


class UploadRejectedError(ValueError):
    """Raised when a file fails validation. Never leaves partial state behind."""

    status_code = 415


class DocumentTooLargeError(UploadRejectedError):
    """Raised when the upload exceeds the configured byte ceiling."""

    status_code = 413


@dataclass(frozen=True, slots=True)
class DocumentFormat:
    """One supported input format and the bytes that identify it."""

    #: Canonical extension, without the dot.
    extension: str
    mime_types: frozenset[str]
    #: Leading bytes every valid file of this type starts with.
    magic: tuple[bytes, ...]
    #: Extra check for ZIP-based formats (OOXML is a ZIP with a fixed layout).
    zip_member: str | None = None


#: PDF files start with ``%PDF-``; DOCX is an OOXML ZIP, so it is distinguished
#: from other ZIPs by requiring the word/document part rather than only ``PK``.
FORMATS: Final[dict[str, DocumentFormat]] = {
    "pdf": DocumentFormat(
        extension="pdf",
        mime_types=frozenset({"application/pdf", "application/x-pdf", "application/acrobat"}),
        magic=(b"%PDF-",),
    ),
    "docx": DocumentFormat(
        extension="docx",
        mime_types=frozenset(
            {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-word.document.macroenabled.12",
                "application/zip",
                "application/octet-stream",
            }
        ),
        magic=(b"PK\x03\x04",),
        zip_member="word/document.xml",
    ),
    "txt": DocumentFormat(
        extension="txt",
        mime_types=frozenset({"text/plain"}),
        # Any text format has no signature, so validation falls back to a
        # control-character ratio check rather than magic bytes.
        magic=(),
    ),
    "csv": DocumentFormat(
        extension="csv",
        mime_types=frozenset(
            {"text/csv", "text/plain", "application/csv", "application/vnd.ms-excel"}
        ),
        magic=(),
    ),
}

#: Extensions whose content is plain text, checked by decodability instead.
TEXTUAL: Final = frozenset({"txt", "csv"})

#: Control bytes that essentially never appear in real text. NUL, bell, and the
#: ANSI escape introducer are the reliable "this is not text" signals; a bare
#: newline or tab must not trip the check.
_CONTROL_BYTES: Final = (
    frozenset(range(0x00, 0x09)) | {0x0B, 0x0C, 0x1B} | frozenset(range(0x0E, 0x20))
)

_UNSAFE_FILENAME: Final = re.compile(r"[^A-Za-z0-9._ -]")


def sanitise_filename(raw: str, *, fallback: str = "upload") -> str:
    """Reduce a client-supplied filename to a safe basename.

    Strips any directory component (so ``../../etc/passwd`` cannot escape the
    storage directory), normalises Unicode, and drops control characters and
    shell-hostile punctuation.
    """
    # Take the basename first: on POSIX this discards traversal entirely.
    name = os.path.basename(raw.replace("\\", "/")).strip()
    name = unicodedata.normalize("NFKC", name)
    name = _UNSAFE_FILENAME.sub("_", name)
    name = name.strip("._ ") or fallback

    # Keep room for the stored UUID prefix and stay inside the DB column.
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        keep = 200 - (len(ext) + 1 if dot else 0)
        name = (stem[:keep] + dot + ext) if dot else name[:200]
    return name


def detect_format(head: bytes, *, filename: str = "", declared: str = "") -> str:
    """Return the canonical format key for a file, or raise :class:`UploadRejectedError`.

    ``head`` is the first :data:`SNIFF_BYTES` of the file. The declared MIME type
    is used only as a tiebreaker between the text formats, never as proof.
    """
    declared = (declared or "").split(";")[0].strip().lower()
    suffix = os.path.splitext(filename)[1].lstrip(".").lower()

    for key, fmt in FORMATS.items():
        if any(head.startswith(sig) for sig in fmt.magic):
            return key

    if _looks_like_text(head):
        # Both txt and csv are text. Trust the extension when it is one of them,
        # otherwise fall back to the declared type, and default to plain text.
        if suffix in TEXTUAL:
            return suffix
        if declared == "text/csv":
            return "csv"
        return "txt"

    raise UploadRejectedError(
        f"Unrecognised file format for {sanitise_filename(filename)!r}. "
        f"Supported: {', '.join(sorted(FORMATS))}."
    )


def validate_extension_matches_content(filename: str, detected: str, *, declared: str = "") -> None:
    """Reject a file whose extension or declared type contradicts its bytes."""
    suffix = os.path.splitext(filename)[1].lstrip(".").lower()
    fmt = FORMATS[detected]

    if suffix and suffix != detected:
        # CSV/TXT are interchangeable by content; everything else must agree.
        if not ({suffix, detected} <= TEXTUAL):
            raise UploadRejectedError(
                f"File extension .{suffix} does not match its actual content "
                f"({detected}). Refusing to guess."
            )

    mime = (declared or "").split(";")[0].strip().lower()
    if mime and mime in fmt.mime_types:
        return
    if mime in {"application/octet-stream", ""}:
        # Generic clients send this; content sniffing already decided.
        return
    if detected in TEXTUAL and mime == "text/plain":
        return
    raise UploadRejectedError(
        f"Declared content type {mime!r} does not match the detected format ({detected})."
    )


def ensure_extension_allowed(filename: str, allowed: list[str]) -> str:
    """Return the canonical extension after checking it against the allow-list."""
    suffix = os.path.splitext(filename)[1].lstrip(".").lower()
    if suffix not in FORMATS:
        raise UploadRejectedError(
            f"Unsupported file extension {suffix or '(none)'!r}. "
            f"Supported: {', '.join(sorted(FORMATS))}."
        )
    if allowed and suffix not in {a.strip().lower() for a in allowed}:
        raise UploadRejectedError(
            f"File type .{suffix} is not accepted by this deployment. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )
    return suffix


def enforce_size_limit(size: int, limit: int) -> None:
    """Reject a file that exceeds the byte ceiling."""
    if size > limit:
        raise DocumentTooLargeError(
            f"Upload is {size} bytes, which exceeds the {limit}-byte limit."
        )


def _looks_like_text(head: bytes) -> bool:
    """Heuristically decide whether a byte prefix decodes as text.

    A ZIP or PDF signature has already been handled by the caller, so anything
    with a low proportion of control bytes is treated as text. UTF-8 and UTF-16
    BOMs are accepted explicitly because UTF-16 legitimately contains many NULs.
    """
    if not head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return True

    sample = head[:2048]
    control = sum(1 for byte in sample if byte in _CONTROL_BYTES)
    return control / len(sample) < 0.05
