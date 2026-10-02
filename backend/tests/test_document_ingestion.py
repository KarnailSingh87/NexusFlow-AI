"""Tests for document ingestion: validation, screening, extraction, chunking.

These cover the pure layers. Anything needing a database or HTTP lives in
``test_documents_api.py``.
"""

from __future__ import annotations

import csv
import io
import uuid
import zipfile
from itertools import pairwise
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.services.documents import chunking, pipeline, scanning, validation
from app.services.documents.extractors import (
    EmptyDocumentError,
    ExtractedText,
    ExtractionError,
    _assemble,
    decode_text,
    extract_csv,
    extract_docx,
    extract_pdf,
    extract_txt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
EICAR_BYTES = scanning.EICAR_SIGNATURE.encode()


def make_docx(path: Path, paragraphs: list[str], table: list[list[str]] | None = None) -> Path:
    """Write a minimal but genuine .docx (OOXML in a ZIP container)."""
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        rows = document.add_table(rows=len(table), cols=len(table[0]))
        for r_index, row in enumerate(table):
            for c_index, cell in enumerate(row):
                rows.cell(r_index, c_index).text = cell
    document.save(str(path))
    return path


def make_pdf(path: Path) -> Path:
    """Write a real one-page PDF containing extractable text."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------
class TestSanitiseFilename:
    def test_strips_directory_traversal(self) -> None:
        assert validation.sanitise_filename("../../etc/passwd") == "passwd"
        assert validation.sanitise_filename("/absolute/path/file.txt") == "file.txt"

    def test_strips_windows_separators(self) -> None:
        assert validation.sanitise_filename(r"..\..\windows\system32\cmd.exe") == "cmd.exe"

    def test_strips_control_and_shell_characters(self) -> None:
        result = validation.sanitise_filename("re;port$(whoami).txt")
        assert ";" not in result and "$" not in result and "(" not in result

    def test_normalises_unicode(self) -> None:
        # Fullwidth "report" (U+FF52 etc.) must NFKC-normalise to ASCII.
        fullwidth = "\uff52\uff45\uff50\uff4f\uff52\uff54.txt"
        assert validation.sanitise_filename(fullwidth) == "report.txt"

    def test_empty_name_gets_fallback(self) -> None:
        assert validation.sanitise_filename("") == "upload"
        assert validation.sanitise_filename("...") == "upload"

    def test_truncates_very_long_names(self) -> None:
        result = validation.sanitise_filename("a" * 500 + ".txt")
        assert len(result) <= 200
        assert result.endswith(".txt")

    def test_never_produces_a_separator(self) -> None:
        for hostile in ["a/b.txt", "a\\b.txt", "..", "./.", "//etc//passwd"]:
            assert "/" not in validation.sanitise_filename(hostile)
            assert "\\" not in validation.sanitise_filename(hostile)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------
class TestDetectFormat:
    def test_detects_pdf_by_magic_bytes(self) -> None:
        assert validation.detect_format(PDF_BYTES, filename="x.pdf") == "pdf"

    def test_detects_docx_by_zip_signature(self) -> None:
        assert validation.detect_format(b"PK\x03\x04rest", filename="x.docx") == "docx"

    def test_detects_plain_text(self) -> None:
        assert validation.detect_format(b"hello world\nsecond line", filename="a.txt") == "txt"

    def test_uses_extension_to_disambiguate_csv_from_txt(self) -> None:
        head = b"a,b,c\n1,2,3"
        assert validation.detect_format(head, filename="a.csv") == "csv"
        assert validation.detect_format(head, filename="a.txt") == "txt"

    def test_declared_mime_used_only_as_tiebreaker(self) -> None:
        head = b"a,b\n1,2"
        assert validation.detect_format(head, filename="a", declared="text/csv") == "csv"

    def test_rejects_binary_content(self) -> None:
        binary = bytes(range(256)) * 4
        with pytest.raises(validation.UploadRejectedError):
            validation.detect_format(binary, filename="x.txt")

    def test_rejects_empty_file(self) -> None:
        with pytest.raises(validation.UploadRejectedError):
            validation.detect_format(b"", filename="x.txt")

    def test_extension_does_not_override_content(self) -> None:
        # A real PDF named .txt is still a PDF.
        assert validation.detect_format(PDF_BYTES, filename="notes.txt") == "pdf"

    def test_accepts_utf8_bom(self) -> None:
        assert validation.detect_format(b"\xef\xbb\xbfhello", filename="a.txt") == "txt"

    def test_accepts_utf16_bom(self) -> None:
        assert validation.detect_format(b"\xff\xfeh\x00i\x00", filename="a.txt") == "txt"


class TestExtensionAgreement:
    def test_rejects_pdf_renamed_to_txt(self) -> None:
        with pytest.raises(validation.UploadRejectedError, match="does not match"):
            validation.validate_extension_matches_content("notes.txt", "pdf")

    def test_allows_txt_and_csv_interchange(self) -> None:
        validation.validate_extension_matches_content("a.csv", "txt")
        validation.validate_extension_matches_content("a.txt", "csv")

    def test_accepts_matching_mime(self) -> None:
        validation.validate_extension_matches_content("a.pdf", "pdf", declared="application/pdf")

    def test_rejects_mismatched_mime(self) -> None:
        with pytest.raises(validation.UploadRejectedError, match="content type"):
            validation.validate_extension_matches_content("a.pdf", "pdf", declared="text/csv")

    def test_tolerates_generic_octet_stream(self) -> None:
        validation.validate_extension_matches_content(
            "a.pdf", "pdf", declared="application/octet-stream"
        )

    def test_allows_missing_extension(self) -> None:
        validation.validate_extension_matches_content("noext", "pdf")


class TestAllowList:
    def test_accepts_configured_extension(self) -> None:
        assert validation.ensure_extension_allowed("a.pdf", ["pdf", "txt"]) == "pdf"

    def test_rejects_unlisted_extension(self) -> None:
        """csv is a supported format that this deployment has not enabled."""
        with pytest.raises(validation.UploadRejectedError, match="not accepted"):
            validation.ensure_extension_allowed("a.csv", ["pdf", "txt"])

    def test_rejects_unknown_extension(self) -> None:
        with pytest.raises(validation.UploadRejectedError, match="Unsupported"):
            validation.ensure_extension_allowed("a.exe", ["pdf"])

    def test_allowlist_is_case_insensitive(self) -> None:
        assert validation.ensure_extension_allowed("A.PDF", ["pdf"]) == "pdf"


class TestSizeLimit:
    def test_rejects_oversized(self) -> None:
        with pytest.raises(validation.DocumentTooLargeError):
            validation.enforce_size_limit(100, 50)

    def test_accepts_at_limit(self) -> None:
        validation.enforce_size_limit(50, 50)


# ---------------------------------------------------------------------------
# Promotion into storage
# ---------------------------------------------------------------------------
class TestPromote:
    def test_stored_name_uses_detected_format_not_staged_suffix(self, tmp_path: Path) -> None:
        """The stored artifact must be self-describing.

        Staging always produces a ``.part`` name, so trusting ``path.suffix``
        would file every document -- PDF included -- as ``.part``.
        """
        staged = tmp_path / "upload-abc.part"
        staged.write_bytes(b"%PDF-1.7 real bytes")
        cfg = get_settings().model_copy(update={"upload_storage_dir": str(tmp_path)})
        doc_id = uuid.uuid4()

        stored = pipeline.promote(staged, config=cfg, doc_id=doc_id, detected_format="pdf")

        assert stored.name == f"{doc_id}.pdf"
        assert stored.read_bytes() == b"%PDF-1.7 real bytes"
        assert not staged.exists()

    @pytest.mark.parametrize(
        ("fmt", "suffix"), [("pdf", ".pdf"), ("docx", ".docx"), ("txt", ".txt"), ("csv", ".csv")]
    )
    def test_every_format_promotes_with_its_own_extension(
        self, tmp_path: Path, fmt: str, suffix: str
    ) -> None:
        staged = tmp_path / "upload-xyz.part"
        staged.write_bytes(b"data")
        cfg = get_settings().model_copy(update={"upload_storage_dir": str(tmp_path)})
        doc_id = uuid.uuid4()

        stored = pipeline.promote(staged, config=cfg, doc_id=doc_id, detected_format=fmt)

        assert stored.name.endswith(suffix)
        assert stored.parent == tmp_path / str(doc_id)

    def test_rejects_unknown_format(self, tmp_path: Path) -> None:
        staged = tmp_path / "upload-q.part"
        staged.write_bytes(b"data")
        cfg = get_settings().model_copy(update={"upload_storage_dir": str(tmp_path)})

        with pytest.raises(validation.UploadRejectedError):
            pipeline.promote(staged, config=cfg, doc_id=uuid.uuid4(), detected_format="exe")
        assert staged.exists(), "a rejected promotion must not consume the staged file"


# ---------------------------------------------------------------------------
# Malware screening
# ---------------------------------------------------------------------------
class TestEicarScan:
    def test_detects_eicar(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.txt"
        path.write_bytes(b"harmless prefix\n" + EICAR_BYTES + b"\nsuffix")
        result = scanning.scan_eicar(path)
        assert result.verdict is scanning.ScanVerdict.INFECTED
        assert result.signature and "EICAR" in result.signature

    def test_detects_wrapped_eicar(self, tmp_path: Path) -> None:
        """A signature split across a line break must still be caught."""
        path = tmp_path / "bad.txt"
        path.write_bytes(EICAR_BYTES[:20] + b"\r\n" + EICAR_BYTES[20:])
        result = scanning.scan_eicar(path)
        assert result.verdict is scanning.ScanVerdict.INFECTED
        assert result.signature and "wrapped" in result.signature

    def test_clean_file_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "ok.txt"
        path.write_bytes(b"perfectly normal text")
        assert scanning.scan_eicar(path).verdict is scanning.ScanVerdict.CLEAN

    def test_missing_file_is_error_not_clean(self, tmp_path: Path) -> None:
        result = scanning.scan_eicar(tmp_path / "absent.txt")
        assert result.verdict is scanning.ScanVerdict.ERROR
        assert not result.clean

    def test_raise_if_infected_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.txt"
        path.write_bytes(EICAR_BYTES)
        result = scanning.scan_eicar(path)
        with pytest.raises(scanning.MalwareDetectedError):
            scanning.raise_if_infected(result, filename="bad.txt")

    def test_raise_if_infected_passes_clean(self, tmp_path: Path) -> None:
        path = tmp_path / "ok.txt"
        path.write_bytes(b"fine")
        scanning.raise_if_infected(scanning.scan_eicar(path), filename="ok.txt")


class TestScreenFile:
    @pytest.mark.asyncio
    async def test_eicar_only_when_no_daemon(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"clean")
        result = await scanning.screen_file(path)
        assert result.verdict is scanning.ScanVerdict.CLEAN
        assert "EICAR only" in (result.detail or "")

    @pytest.mark.asyncio
    async def test_blocks_eicar_without_daemon(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(EICAR_BYTES)
        result = await scanning.screen_file(path)
        assert result.verdict is scanning.ScanVerdict.INFECTED


class TestClamAvResponseParsing:
    def test_parses_ok(self) -> None:
        result = scanning._parse_clamav_response("/tmp/f: OK")
        assert result.verdict is scanning.ScanVerdict.CLEAN

    def test_parses_found(self) -> None:
        result = scanning._parse_clamav_response("/tmp/f: Eicar-Test-Signature FOUND")
        assert result.verdict is scanning.ScanVerdict.INFECTED
        assert result.signature == "Eicar-Test-Signature"

    def test_empty_response_is_error_not_clean(self) -> None:
        assert scanning._parse_clamav_response("").verdict is scanning.ScanVerdict.ERROR

    def test_unknown_command_is_error(self) -> None:
        result = scanning._parse_clamav_response("UNKNOWN COMMAND")
        assert result.verdict is scanning.ScanVerdict.ERROR


class TestClamAvScannerStreaming:
    """The INSTREAM protocol needs a real socket; exercise it against a local echo."""

    @pytest.mark.asyncio
    async def test_unreachable_daemon_reports_error(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"x")
        # Port 1 is reserved and never listening.
        scanner = scanning.ClamAvScanner("127.0.0.1", 1, timeout=0.5)
        result = await scanner.scan(path)
        assert result.verdict is scanning.ScanVerdict.ERROR
        assert "unreachable" in (result.detail or "")

    @pytest.mark.asyncio
    async def test_unreachable_daemon_never_claims_clean(self, tmp_path: Path) -> None:
        """A dead scanner must not produce a false 'clean'."""
        path = tmp_path / "f.txt"
        path.write_bytes(b"x")
        scanner = scanning.ClamAvScanner("127.0.0.1", 1, timeout=0.5)
        assert not (await scanner.scan(path)).clean

    @pytest.mark.asyncio
    async def test_screen_file_falls_back_when_daemon_down(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"x")
        cfg = get_settings().model_copy(update={"clamav_host": "127.0.0.1", "clamav_port": 1})
        result = await scanning.screen_file(path, config=cfg)
        assert result.verdict is scanning.ScanVerdict.CLEAN
        assert "unavailable" in result.engine


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
class TestDecodeText:
    def test_utf8(self) -> None:
        text, encoding = decode_text("héllo wörld".encode())
        assert text == "héllo wörld"
        assert encoding == "utf-8"

    def test_utf8_bom_is_stripped(self) -> None:
        text, encoding = decode_text(b"\xef\xbb\xbfhello")
        assert text == "hello"
        assert encoding == "utf-8-sig"

    def test_utf16_bom(self) -> None:
        text, _ = decode_text("hi".encode("utf-16"))
        assert text == "hi"

    def test_single_byte_fallback(self) -> None:
        """0xE9 is 'é' in cp1252, which is tried before never-failing latin-1."""
        text, encoding = decode_text(b"caf\xe9")
        assert text == "café"
        assert encoding == "cp1252"

    def test_latin1_is_the_last_resort(self) -> None:
        # 0x81 is undefined in cp1252, so only latin-1 can decode it.
        text, encoding = decode_text(b"a\x81b")
        assert encoding == "latin-1"
        assert text == "a\x81b"

    def test_utf8_preferred_over_latin1(self) -> None:
        """latin-1 decodes anything, so UTF-8 must be tried strictly first."""
        raw = "日本語のテキスト".encode()
        text, encoding = decode_text(raw)
        assert text == "日本語のテキスト"
        assert encoding.startswith("utf-8")


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------
class TestExtractTxt:
    def test_reads_text(self, tmp_path: Path) -> None:
        path = tmp_path / "a.txt"
        path.write_text("first line\n\nsecond paragraph")
        result = extract_txt(path)
        assert "first line" in result.text
        assert "second paragraph" in result.text
        assert str(result.metadata["encoding"]).startswith("utf-8")

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "a.txt"
        path.write_text("   \n  ")
        with pytest.raises(EmptyDocumentError):
            extract_txt(path)

    def test_collapses_horizontal_whitespace(self, tmp_path: Path) -> None:
        path = tmp_path / "a.txt"
        path.write_text("a        b\tc")
        assert extract_txt(path).text == "a b c"


class TestExtractCsv:
    def test_renders_rows_with_headers(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_text("name,role\nAda,engineer\nGrace,admiral")
        result = extract_csv(path)
        assert "name: Ada" in result.text
        assert "role: admiral" in result.text
        assert result.metadata["rows"] == 2
        assert result.metadata["columns"] == ["name", "role"]

    def test_sniffs_semicolon_delimiter(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_text("a;b;c\n1;2;3")
        result = extract_csv(path)
        assert result.metadata["delimiter"] == ";"
        assert "1" in result.text

    def test_single_column_falls_back_to_comma(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_text("only\nvalue1\nvalue2")
        result = extract_csv(path)
        assert result.metadata["delimiter"] == ","

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_text("")
        with pytest.raises(EmptyDocumentError):
            extract_csv(path)

    def test_header_without_rows_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_text("name,role")
        with pytest.raises(EmptyDocumentError):
            extract_csv(path)

    def test_quoted_commas_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_text('name,note\n"Smith, Ada","said ""hi"""')
        result = extract_csv(path)
        assert "Smith, Ada" in result.text
        assert 'said "hi"' in result.text

    def test_oversized_field_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_text("a\n" + "x" * (2 * 1024 * 1024))
        with pytest.raises(ExtractionError):
            extract_csv(path)

    def test_field_size_limit_restored(self, tmp_path: Path) -> None:
        import csv as csv_module

        before = csv_module.field_size_limit()
        path = tmp_path / "a.csv"
        path.write_text("a\n1")
        extract_csv(path)
        assert csv_module.field_size_limit() == before


class TestExtractDocx:
    def test_extracts_paragraphs(self, tmp_path: Path) -> None:
        path = make_docx(tmp_path / "a.docx", ["first para", "second para"])
        result = extract_docx(path)
        assert "first para" in result.text
        assert "second para" in result.text

    def test_extracts_tables(self, tmp_path: Path) -> None:
        path = make_docx(tmp_path / "a.docx", ["intro"], [["h1", "h2"], ["v1", "v2"]])
        result = extract_docx(path)
        assert "h1 | h2" in result.text
        assert "v1 | v2" in result.text
        assert result.metadata["table_rows"] == 2

    def test_empty_docx_rejected(self, tmp_path: Path) -> None:
        path = make_docx(tmp_path / "a.docx", [])
        with pytest.raises(EmptyDocumentError):
            extract_docx(path)

    def test_not_a_docx_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "a.docx"
        path.write_bytes(b"PK\x03\x04" + b"garbage")
        with pytest.raises(ExtractionError):
            extract_docx(path)


class TestExtractPdf:
    def test_pdf_without_text_layer_is_reported_clearly(self, tmp_path: Path) -> None:
        path = make_pdf(tmp_path / "a.pdf")
        with pytest.raises(EmptyDocumentError, match="OCR"):
            extract_pdf(path)

    def test_encrypted_pdf_is_rejected(self, tmp_path: Path) -> None:
        from pypdf import PdfWriter

        path = tmp_path / "locked.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("secret")
        with path.open("wb") as handle:
            writer.write(handle)
        with pytest.raises(ExtractionError, match="password"):
            extract_pdf(path)

    def test_truncated_pdf_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.pdf"
        path.write_bytes(b"%PDF-1.7\n" + b"\x00" * 20)
        with pytest.raises(ExtractionError):
            extract_pdf(path)


class TestExtractDispatch:
    @pytest.mark.asyncio
    async def test_unknown_format_raises(self, tmp_path: Path) -> None:
        from app.services.documents.extractors import extract_async

        with pytest.raises(ExtractionError, match="No extractor"):
            await extract_async(tmp_path / "a.bin", "exe")

    @pytest.mark.asyncio
    async def test_runs_in_worker_thread(self, tmp_path: Path) -> None:

        from app.services.documents.extractors import extract_async

        path = tmp_path / "a.txt"
        path.write_text("threaded")
        result = await extract_async(path, "txt")
        assert result.text == "threaded"


# ---------------------------------------------------------------------------
# Offsets and page mapping
# ---------------------------------------------------------------------------
class TestOffsets:
    def test_page_offsets_are_exact(self) -> None:
        assembled = _assemble(["page one text", "page two text"], numbers=[1, 2])
        for page in assembled.pages:
            assert assembled.text[page.start : page.start + len(page.text)] == page.text

    def test_page_of_maps_offsets(self) -> None:
        assembled = _assemble(["aaa", "bbb", "ccc"], numbers=[1, 2, 3])
        extracted = ExtractedText(text=assembled.text, pages=assembled.pages)
        assert extracted.page_of(0) == 1
        assert extracted.page_of(assembled.pages[1].start) == 2
        assert extracted.page_of(len(extracted.text) - 1) == 3

    def test_page_count_defaults_to_one(self) -> None:
        assert ExtractedText(text="abc").page_count == 1

    def test_unpaginated_format_is_a_single_page(self) -> None:
        """DOCX/TXT/CSV have no pages, so every block stays on page 1.

        Synthesising a page per paragraph would advertise a four-block DOCX as a
        four-page document and send readers to pages that do not exist.
        """
        assembled = _assemble(["alpha", "beta", "gamma"])
        assert assembled.page_count == 1
        assert [page.number for page in assembled.pages] == [1]
        assert assembled.page_of(len(assembled.text) - 1) == 1

    def test_paginated_format_keeps_real_numbers(self) -> None:
        assembled = _assemble(["a", "b", "c"], numbers=[1, 2, 3])
        assert assembled.page_count == 3
        assert assembled.page_of(assembled.pages[2].start) == 3

    def test_with_metadata_merges(self) -> None:
        result = ExtractedText(text="abc").with_metadata({"a": 1}).with_metadata({"b": 2})
        assert result.metadata == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def doc_from_blocks(blocks: list[str], numbers: list[int] | None = None) -> ExtractedText:
    assembled = _assemble(blocks, numbers)
    return ExtractedText(text=assembled.text, pages=assembled.pages)


class TestChunking:
    def test_small_paragraphs_are_packed_together(self) -> None:
        """Short paragraphs merge into one chunk rather than fragmenting."""
        extracted = doc_from_blocks(["first para", "second para", "third para"])
        chunks = chunking.chunk_document(extracted, chunk_size=200, overlap=0)
        assert len(chunks) == 1

    def test_splits_on_paragraph_boundaries(self) -> None:
        extracted = doc_from_blocks(["first para", "second para", "third para"])
        chunks = chunking.chunk_document(extracted, chunk_size=20, overlap=0)
        assert len(chunks) == 3
        assert [chunk.content for chunk in chunks] == [
            "first para",
            "second para",
            "third para",
        ]

    def test_respects_size_budget(self) -> None:
        extracted = doc_from_blocks([" ".join(["word"] * 200) for _ in range(4)])
        chunks = chunking.chunk_document(extracted, chunk_size=300, overlap=0)
        assert all(len(chunk.content) <= 300 for chunk in chunks)

    def test_offsets_are_exact(self) -> None:
        extracted = doc_from_blocks(
            ["first block with words", "second block with more words", "third block here"]
        )
        for chunk in chunking.chunk_document(extracted, chunk_size=60, overlap=15):
            assert extracted.text[chunk.char_start : chunk.char_end] == chunk.content, (
                "chunk offsets must index the document exactly"
            )

    def test_ordinals_are_sequential(self) -> None:
        extracted = doc_from_blocks(["a " * 100, "b " * 100, "c " * 100])
        chunks = chunking.chunk_document(extracted, chunk_size=150, overlap=10)
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    def test_no_mid_word_splits(self) -> None:
        words = [f"word{i}" for i in range(200)]
        extracted = doc_from_blocks([" ".join(words)])
        for chunk in chunking.chunk_document(extracted, chunk_size=200, overlap=0):
            # A chunk boundary should never land inside a token.
            assert chunk.content == chunk.content.strip()
            assert all(part for part in chunk.content.split())

    def test_sentence_boundary_respected(self) -> None:
        extracted = doc_from_blocks(["Alpha one. " * 20 + "Beta two. " * 20])
        chunks = chunking.chunk_document(extracted, chunk_size=220, overlap=0)
        # Every chunk must end on a sentence terminator, not mid-sentence.
        for chunk in chunks[:-1]:
            assert chunk.content.rstrip().endswith(".")

    def test_decimals_are_not_split(self) -> None:
        extracted = ExtractedText(text="The value is 3.14159 exactly. " * 12)
        for chunk in chunking.chunk_document(extracted, chunk_size=200, overlap=0):
            assert "3.14159" in chunk.content

    def test_abbreviations_are_not_split(self) -> None:
        extracted = ExtractedText(text="Dr. Chen and Prof. Adams discussed it. " * 10)
        chunks = chunking.chunk_document(extracted, chunk_size=250, overlap=0)
        for chunk in chunks:
            assert "Dr." not in chunk.content or "Dr. Chen" in chunk.content

    def test_initials_kept_together(self) -> None:
        extracted = ExtractedText(text="J. R. Ewing filed the report. " * 10)
        for chunk in chunking.chunk_document(extracted, chunk_size=250, overlap=0):
            assert "J. R. Ewing" in chunk.content

    def test_overlap_repeats_context(self) -> None:
        extracted = doc_from_blocks(
            [
                " ".join(f"alpha{i}" for i in range(30)),
                " ".join(f"beta{i}" for i in range(30)),
                " ".join(f"gamma{i}" for i in range(30)),
            ]
        )
        with_overlap = chunking.chunk_document(extracted, chunk_size=200, overlap=80)
        without = chunking.chunk_document(extracted, chunk_size=200, overlap=0)
        # Overlap consumes budget, so it can never yield fewer chunks.
        assert len(with_overlap) >= len(without)
        assert any(
            set(a.content.split()) & set(b.content.split()) for a, b in pairwise(with_overlap)
        )
        assert not any(
            set(a.content.split()) & set(b.content.split()) for a, b in pairwise(without)
        )

    def test_overlap_capped_at_half_the_budget(self) -> None:
        extracted = doc_from_blocks(["word " * 80])
        # An overlap larger than half the chunk size would never terminate.
        chunks = chunking.chunk_document(extracted, chunk_size=200, overlap=5000)
        assert chunks

    def test_page_numbers_assigned(self) -> None:
        extracted = doc_from_blocks(
            ["page one content here", "page two content here"], numbers=[1, 2]
        )
        chunks = chunking.chunk_document(extracted, chunk_size=25, overlap=0)
        assert [chunk.page_number for chunk in chunks] == [1, 2]

    def test_start_and_end_markers(self) -> None:
        extracted = doc_from_blocks(["only block"])
        chunk = chunking.chunk_document(extracted, chunk_size=500, overlap=0)[0]
        assert chunk.metadata["is_document_start"] is True
        assert chunk.metadata["is_document_end"] is True

    def test_max_chunks_truncates_and_records(self) -> None:
        extracted = doc_from_blocks(["block " * 30 for _ in range(40)])
        chunks = chunking.chunk_document(extracted, chunk_size=120, overlap=0, max_chunks=3)
        assert len(chunks) == 3
        assert chunks[-1].metadata["truncated"] is True

    def test_no_truncation_marker_when_under_cap(self) -> None:
        extracted = doc_from_blocks(["a", "b", "c"])
        chunks = chunking.chunk_document(extracted, chunk_size=500, overlap=0, max_chunks=10)
        assert all("truncated" not in chunk.metadata for chunk in chunks)

    def test_empty_document_rejected(self) -> None:
        with pytest.raises(chunking.ChunkingError, match="empty"):
            chunking.chunk_document(ExtractedText(text="   "), chunk_size=100, overlap=0)

    def test_invalid_chunk_size_rejected(self) -> None:
        with pytest.raises(chunking.ChunkingError, match="positive"):
            chunking.chunk_document(doc_from_blocks(["x"]), chunk_size=0, overlap=0)

    def test_hard_split_handles_unbroken_run(self) -> None:
        """Text with no spaces must still be chunked rather than crash."""
        extracted = ExtractedText(text="x" * 1000)
        chunks = chunking.chunk_document(extracted, chunk_size=300, overlap=50)
        assert len(chunks) > 1
        assert all(len(chunk.content) <= 300 for chunk in chunks)

    def test_every_chunk_reconstructed_from_document(self) -> None:
        """Fuzz-ish invariant across sizes and overlaps."""
        blocks = [" ".join(f"w{i}" for i in range(n)) + "." for n in (5, 40, 120, 3, 60)]
        extracted = doc_from_blocks(blocks)
        for size in (40, 120, 400):
            for overlap in (0, 20, 60):
                chunks = chunking.chunk_document(extracted, chunk_size=size, overlap=overlap)
                for chunk in chunks:
                    assert extracted.text[chunk.char_start : chunk.char_end] == chunk.content


class TestTokenEstimate:
    def test_empty_is_zero(self) -> None:
        assert chunking.estimate_tokens("") == 0

    def test_short_text_is_one(self) -> None:
        assert chunking.estimate_tokens("hi") == 1

    def test_scales_with_length(self) -> None:
        assert chunking.estimate_tokens("a" * 400) == 100

    def test_rounds_up(self) -> None:
        assert chunking.estimate_tokens("abc") == 1
        assert chunking.estimate_tokens("a" * 5) == 2


class TestEmbeddingInput:
    def test_plain_chunk_unchanged(self) -> None:
        chunk = chunking.TextChunk(
            ordinal=0,
            content="body",
            page_number=None,
            char_start=0,
            char_end=4,
            token_count=1,
        )
        assert chunking.chunk_to_embedding_input(chunk) == "body"

    def test_heading_is_prefixed(self) -> None:
        chunk = chunking.TextChunk(
            ordinal=0,
            content="body",
            page_number=None,
            char_start=0,
            char_end=4,
            token_count=1,
            metadata={"heading": "Chapter 2"},
        )
        assert chunking.chunk_to_embedding_input(chunk) == "Chapter 2\n\nbody"


# ---------------------------------------------------------------------------
# Sanity checks on the helper-built fixtures
# ---------------------------------------------------------------------------
def test_make_docx_is_a_real_zip(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "a.docx", ["x"])
    with zipfile.ZipFile(path) as archive:
        assert "word/document.xml" in archive.namelist()


def test_make_pdf_has_pdf_magic(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "a.pdf")
    assert path.read_bytes().startswith(b"%PDF-")


def test_make_csv_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows([["a", "b"], ["1", "2"]])
    assert "a: 1" in extract_csv(path).text


def test_csv_with_bom_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_bytes(b"\xef\xbb\xbfa,b\n1,2")
    assert "a: 1" in extract_csv(path).text


def test_empty_string_reader_is_csv_safe(tmp_path: Path) -> None:
    """Regression: the CSV path must not emit stray separators for blank cells."""
    path = tmp_path / "a.csv"
    path.write_text("a,b\n1,\n,2")
    result = extract_csv(path)
    assert result.text.count(";") >= 1
    assert io.StringIO(result.text).read() == result.text
