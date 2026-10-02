"""Turn extracted document text into retrieval-sized semantic blocks.

Naive fixed-width splitting cuts mid-sentence, which wrecks retrieval: the
half-sentence fragment that lands in a chunk has no meaning on its own. This
chunker splits on semantic boundaries first and only falls back to a hard cut
when a single block is larger than the budget.

Strategy, in order of preference:

1. **Paragraph** — blank-line separated blocks are the primary unit.
2. **Sentence** — oversized paragraphs split on sentence terminators. Boundaries
   require trailing whitespace, so "3.14" and "Dr. Chen" stay intact.
3. **Word boundary** — a single sentence longer than the budget splits on
   whitespace, never mid-word.
4. **Hard cut** — last resort for pathological runs with no whitespace.

Adjacent chunks share :data:`overlap` characters of context so a fact that
straddles a boundary is retrievable from either side.

Every chunk records character offsets that index the document text *exactly*.
The text is not rewritten here — extractors normalise whitespace when they build
it — so ``text[chunk.char_start :chunk.char_end] == chunk.content`` holds, and
offsets can be used to trace a retrieval back to the source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from app.core.logging import get_logger
from app.services.documents.extractors import ExtractedText

logger = get_logger(__name__)

#: Rough characters-per-token ratio for English prose. Good enough for budgeting
#: and avoids pulling in a tokenizer for every format.
CHARS_PER_TOKEN: Final = 4

#: Sentence boundary: punctuation then whitespace, or a CJK terminator. Requiring
#: whitespace stops decimals and abbreviations from being treated as boundaries.
_SENTENCE_SPLIT: Final = re.compile("(?<=[.!?])[ \t]+|[\u3002\uff01\uff1f][ \t]*\n?")
_PARAGRAPH_SPLIT: Final = re.compile(r"\n[ \t]*\n+")

#: An indexed block: its text plus where that text starts in the document.
_Span = tuple[str, int]


class ChunkingError(ValueError):
    """Raised when a document cannot be split into usable chunks."""

    status_code = 422


@dataclass(frozen=True, slots=True)
class TextChunk:
    """One semantic block of a document, ready for embedding and storage."""

    ordinal: int
    content: str
    page_number: int | None
    char_start: int
    char_end: int
    token_count: int
    metadata: dict[str, object] = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    if not text:
        return 0
    return max(1, -(-len(text) // CHARS_PER_TOKEN))


def _trim(block: str, start: int, end: int) -> _Span | None:
    """Return the whitespace-trimmed span of ``block[start:end]`` with offsets."""
    segment = block[start:end]
    if not segment.strip():
        return None
    lead = len(segment) - len(segment.lstrip())
    trimmed = segment.strip()
    begin = start + lead
    return trimmed, begin


def _spans(block: str, boundary: re.Pattern[str], offset: int) -> list[_Span]:
    """Split ``block`` on ``boundary``, returning offsets relative to the document."""
    out: list[_Span] = []
    cursor = 0
    for match in boundary.finditer(block):
        found = _trim(block, cursor, match.start())
        if found is not None:
            out.append((found[0], offset + found[1]))
        cursor = match.end()
    tail = _trim(block, cursor, len(block))
    if tail is not None:
        out.append((tail[0], offset + tail[1]))
    return out


#: Tokens that end in a period but do not end a sentence. Splitting on these
#: separates "Dr." from "Chen", which damages retrieval far more than the small
#: loss in split quality is worth.
_ABBREVIATIONS: Final = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "mt",
        "rev",
        "hon",
        "gen",
        "col",
        "capt",
        "lt",
        "sgt",
        "maj",
        "adm",
        "gov",
        "sen",
        "rep",
        "pres",
        "inc",
        "ltd",
        "llc",
        "co",
        "corp",
        "dept",
        "univ",
        "assn",
        "bros",
        "mfg",
        "vs",
        "etc",
        "al",
        "approx",
        "est",
        "fig",
        "figs",
        "eq",
        "no",
        "nos",
        "vol",
        "ch",
        "pp",
        "ed",
        "eds",
        "trans",
        "ref",
        "refs",
        "min",
        "max",
        "avg",
        "sec",
        "e.g",
        "i.e",
        "cf",
        "viz",
        "ibid",
        "op",
        "cit",
    }
)

#: A single capital letter before the period is an initial ("J. Smith"), which is
#: also not a sentence boundary.
_INITIAL: Final = re.compile(r"(?:^|[\s(])([A-Z])\.$")


def _ends_with_abbreviation(text: str) -> bool:
    """Whether ``text`` ends in an abbreviation rather than a sentence."""
    match = re.search(r"([\w.]+)\.$", text.strip())
    if match is None:
        return False
    token = match.group(1).lower().rstrip(".")
    if token in _ABBREVIATIONS:
        return True
    # "J." / "U.S." style initials.
    stripped = match.group(1)
    return bool(_INITIAL.search(stripped)) or (
        len(stripped) <= 3 and all(part.isalpha() for part in stripped.split("."))
    )


def _merge_abbreviations(spans: list[_Span]) -> list[_Span]:
    """Rejoin spans that were split on a period belonging to an abbreviation."""
    if len(spans) < 2:
        return spans

    merged: list[_Span] = []
    for text, start in spans:
        if merged and _ends_with_abbreviation(merged[-1][0]):
            previous_text, previous_start = merged[-1]
            # Rejoin with a single space; offsets stay anchored to the two ends.
            merged[-1] = (f"{previous_text} {text}", previous_start)
            continue
        merged.append((text, start))
    return merged


def _pack_words(spans: list[_Span], size: int) -> list[_Span]:
    """Split oversized spans on whitespace, preserving exact offsets."""
    out: list[_Span] = []
    for text, start in spans:
        if len(text) <= size:
            out.append((text, start))
            continue

        current_words: list[tuple[str, int]] = []
        length = 0

        def flush() -> None:
            nonlocal current_words, length
            if not current_words:
                return
            # Rejoin with single spaces; offsets of each word still index the
            # document, so report the span of the words themselves.
            begin = current_words[0][1]
            out.append((" ".join(word for word, _ in current_words), begin))
            current_words = []
            length = 0

        for word, word_start in _word_spans(text, start):
            addition = len(word) + (1 if current_words else 0)
            if current_words and length + addition > size:
                flush()
                addition = len(word)
            current_words.append((word, word_start))
            length += addition
        flush()
    return out


def _word_spans(text: str, offset: int) -> list[_Span]:
    """Yield each whitespace-delimited word with its document offset."""
    words: list[_Span] = []
    for match in re.finditer(r"\S+", text):
        words.append((match.group(0), offset + match.start()))
    return words


def _hard_split(spans: list[_Span], size: int) -> list[_Span]:
    """Last-resort fixed-width cut for text with no usable boundary."""
    out: list[_Span] = []
    for text, start in spans:
        if len(text) <= size:
            out.append((text, start))
            continue
        for offset in range(0, len(text), size):
            out.append((text[offset : offset + size], start + offset))
    return out


def _split_oversized(spans: list[_Span], size: int) -> list[_Span]:
    """Break oversized spans down using progressively coarser boundaries."""
    if all(len(text) <= size for text, _ in spans):
        return spans

    sentence_level: list[_Span] = []
    for text, start in spans:
        if len(text) <= size:
            sentence_level.append((text, start))
            continue
        sentence_level.extend(_merge_abbreviations(_spans(text, _SENTENCE_SPLIT, start)))

    if all(len(text) <= size for text, _ in sentence_level):
        return sentence_level

    word_level = _pack_words(sentence_level, size)
    if all(len(text) <= size for text, _ in word_level):
        return word_level

    return _hard_split(word_level, size)


def _segments(text: str, size: int) -> list[_Span]:
    """Split document text into ordered spans no larger than ``size``."""
    paragraphs: list[_Span] = []
    cursor = 0
    for match in _PARAGRAPH_SPLIT.finditer(text):
        found = _trim(text, cursor, match.start())
        if found is not None:
            paragraphs.append(found)
        cursor = match.end()
    tail = _trim(text, cursor, len(text))
    if tail is not None:
        paragraphs.append(tail)

    return _split_oversized(paragraphs, size)


def _overlap_seed(previous: list[_Span], overlap: int) -> tuple[list[_Span], int]:
    """Pick trailing spans from the previous chunk to repeat into the next one.

    Returns the seed spans and the character offset where the repeated region
    begins in the document, so the next chunk's span stays truthful.
    """
    if overlap <= 0:
        return [], 0
    seed: list[_Span] = []
    length = 0
    for text, start in reversed(previous):
        addition = len(text) + (2 if seed else 0)
        if length + addition > overlap:
            break
        seed.insert(0, (text, start))
        length += addition
    if not seed:
        return [], 0
    return seed, seed[0][1]


def _pack(segments: list[_Span], size: int, overlap: int) -> list[TextChunk]:
    """Greedily pack spans into chunks with overlap between neighbours."""
    chunks: list[TextChunk] = []
    current: list[_Span] = []
    length = 0
    ordinal = 0

    def emit(spans: list[_Span]) -> None:
        nonlocal ordinal
        if not spans:
            return
        content = "\n\n".join(text for text, _ in spans)
        begin = spans[0][1]
        end = spans[-1][1] + len(spans[-1][0])
        chunks.append(
            TextChunk(
                ordinal=ordinal,
                content=content,
                page_number=None,
                char_start=begin,
                char_end=end,
                token_count=estimate_tokens(content),
            )
        )
        ordinal += 1

    for span in segments:
        addition = len(span[0]) + (2 if current else 0)
        if current and length + addition > size:
            previous = list(current)
            seed, _ = _overlap_seed(previous, overlap)
            emit(current)
            current = seed
            length = sum(len(text) for text, _ in seed) + 2 * max(0, len(seed) - 1)
        current.append(span)
        length += len(span[0]) + (2 if len(current) > 1 else 0)
    emit(current)
    return chunks


def chunk_document(
    extracted: ExtractedText,
    *,
    chunk_size: int,
    overlap: int,
    max_chunks: int | None = None,
) -> list[TextChunk]:
    """Split ``extracted`` into semantic chunks.

    Args:
        extracted: Output of the format-specific extractor.
        chunk_size: Target characters per chunk.
        overlap: Characters of trailing context repeated into the next chunk.
        max_chunks: Stop after this many chunks so one huge document cannot
            blow up embedding cost. Truncation is recorded in the final chunk's
            metadata so it is never silent.
    """
    if chunk_size <= 0:
        raise ChunkingError(f"chunk_size must be positive, got {chunk_size}.")
    overlap = max(0, min(overlap, chunk_size // 2))
    if not extracted.text.strip():
        raise ChunkingError("Cannot chunk an empty document.")

    segments = _segments(extracted.text, chunk_size)
    if not segments:
        raise ChunkingError("Document produced no usable text segments.")

    chunks = _pack(segments, chunk_size, overlap)

    truncated = False
    if max_chunks is not None and len(chunks) > max_chunks:
        logger.warning(
            "Chunk cap hit: keeping %d of %d chunks for this document",
            max_chunks,
            len(chunks),
        )
        chunks = chunks[:max_chunks]
        truncated = True

    # Attach page numbers now that each chunk's start offset is final.
    resolved: list[TextChunk] = []
    for chunk in chunks:
        meta: dict[str, object] = {}
        if chunk.ordinal == 0:
            meta["is_document_start"] = True
        if chunk.char_end >= len(extracted.text):
            meta["is_document_end"] = True
        if truncated and chunk.ordinal == len(chunks) - 1:
            meta["truncated"] = True
        resolved.append(
            TextChunk(
                ordinal=chunk.ordinal,
                content=chunk.content,
                page_number=extracted.page_of(chunk.char_start),
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                token_count=chunk.token_count,
                metadata=meta,
            )
        )

    return resolved


def chunk_to_embedding_input(chunk: TextChunk) -> str:
    """Return the text actually sent to the embedding model.

    Chunk metadata is prefixed when present so a retrieved vector carries the
    context needed to answer questions about it, which measurably beats
    embedding the bare fragment.
    """
    heading = chunk.metadata.get("heading")
    if isinstance(heading, str) and heading:
        return f"{heading}\n\n{chunk.content}"
    return chunk.content
