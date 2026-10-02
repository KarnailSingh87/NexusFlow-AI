"""End-to-end tests for the document ingestion endpoints.

These run against a real in-memory SQLite database rather than a mocked session:
chunk persistence, ownership scoping, and cascade deletes are the behaviour that
actually needs proving.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import httpx
import pytest
from app.api.deps import get_config
from app.core.config import Settings, get_settings
from app.db.models import Base, Document, DocumentChunk, DocumentStatus
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_document_ingestion import EICAR_BYTES, PDF_BYTES, make_docx

pytestmark = pytest.mark.asyncio

OWNER_EMAIL = "owner@example.com"
TEXT = "First paragraph of the document.\n\nSecond paragraph with more detail."
CSV_TEXT = "name,role\nAda,engineer\nGrace,admiral"


@pytest.fixture(autouse=True)
def _isolate(app: FastAPI, upload_dir: Path) -> Generator[None, None, None]:
    """Point every request at a private storage directory."""
    base = get_settings()

    def _config() -> Settings:
        return base.model_copy(
            update={
                "upload_storage_dir": str(upload_dir),
                "clamav_host": "",
                # Requests without an X-User-Email header fall back to this, so
                # it must match the identity the upload helper uses.
                "dev_user_email": OWNER_EMAIL,
            }
        )

    app.dependency_overrides[get_config] = _config
    yield
    app.dependency_overrides.pop(get_config, None)


@pytest.fixture
def session_override(app: FastAPI, db_session: AsyncSession) -> Generator[AsyncSession, None, None]:
    """Serve the throwaway database to every request."""
    from app.db.session import get_session

    async def _get() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _get
    yield db_session
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def api(
    app: FastAPI, client: httpx.AsyncClient, session_override: AsyncSession
) -> AsyncIterator[tuple[httpx.AsyncClient, AsyncSession]]:
    yield client, session_override


async def post_upload(
    api_client: httpx.AsyncClient,
    *,
    filename: str,
    content: bytes,
    mime: str = "text/plain",
    email: str = OWNER_EMAIL,
) -> httpx.Response:
    """POST a multipart upload with an explicit owner header."""
    return await api_client.post(
        "/api/v1/documents/upload",
        files={"file": (filename, content, mime)},
        headers={"X-User-Email": email},
    )


class TestUpload:
    async def test_uploads_txt(self, api: tuple[httpx.AsyncClient, AsyncSession]) -> None:
        api_client, _ = api
        response = await post_upload(api_client, filename="notes.txt", content=TEXT.encode())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["filename"] == "notes.txt"
        assert body["format"] == "txt"
        assert body["chunk_count"] >= 1
        assert body["status"] == DocumentStatus.EMBEDDING.value
        assert len(body["checksum_sha256"]) == 64
        assert body["extraction"]["characters"] > 0

    async def test_uploads_csv(self, api: tuple[httpx.AsyncClient, AsyncSession]) -> None:
        api_client, _ = api
        response = await post_upload(
            api_client,
            filename="people.csv",
            content=CSV_TEXT.encode(),
            mime="text/csv",
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["format"] == "csv"
        assert body["extraction"]["metadata"]["rows"] == 2

    async def test_uploads_docx(
        self, api: tuple[httpx.AsyncClient, AsyncSession], tmp_path: Path
    ) -> None:
        api_client, _ = api
        docx_path = make_docx(tmp_path / "report.docx", ["Alpha para", "Beta para"])
        response = await post_upload(
            api_client,
            filename="report.docx",
            content=docx_path.read_bytes(),
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        assert response.status_code == 201, response.text
        assert response.json()["format"] == "docx"

    async def test_uploads_pdf_without_text_is_422(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        """A PDF with no text layer cannot produce chunks, so it is rejected."""
        api_client, _ = api
        response = await post_upload(
            api_client,
            filename="scan.pdf",
            content=PDF_BYTES,
            mime="application/pdf",
        )
        assert response.status_code == 422

    async def test_persists_document_and_chunks(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, session = api
        response = await post_upload(api_client, filename="notes.txt", content=TEXT.encode())
        doc_id = uuid.UUID(response.json()["id"])

        document = await session.get(Document, doc_id)
        assert document is not None
        assert document.filename == "notes.txt"
        assert document.chunk_count >= 1

        chunks = (
            await session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == doc_id)
                .order_by(DocumentChunk.ordinal)
            )
        ).all()
        assert len(chunks) == document.chunk_count
        assert "First paragraph" in chunks[0].content

    async def test_stores_file_on_disk(self, api: tuple[httpx.AsyncClient, AsyncSession]) -> None:
        api_client, session = api
        response = await post_upload(api_client, filename="notes.txt", content=TEXT.encode())
        document = await session.get(Document, uuid.UUID(response.json()["id"]))
        assert document is not None
        stored = Path(document.storage_path)
        assert stored.is_file()
        assert stored.read_text() == TEXT

    async def test_storage_path_is_uuid_scoped(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        """The client filename must never become a path component."""
        api_client, session = api
        response = await post_upload(api_client, filename="../../escape.txt", content=TEXT.encode())
        document = await session.get(Document, uuid.UUID(response.json()["id"]))
        assert document is not None
        stored = Path(document.storage_path)
        assert stored.parent.name == str(document.id)
        assert ".." not in str(stored)
        assert stored.is_file()


class TestUploadRejection:
    async def test_rejects_eicar(self, api: tuple[httpx.AsyncClient, AsyncSession]) -> None:
        api_client, _ = api
        response = await post_upload(api_client, filename="virus.txt", content=EICAR_BYTES)
        assert response.status_code == 422
        assert "malware" in response.json()["error"]["message"].lower()

    async def test_rejects_extension_mismatch(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        """A PDF renamed to .txt must not be parsed as text."""
        api_client, _ = api
        response = await post_upload(api_client, filename="disguised.txt", content=PDF_BYTES)
        assert response.status_code == 415

    async def test_rejects_binary_content(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        response = await post_upload(api_client, filename="blob.txt", content=bytes(range(256)) * 8)
        assert response.status_code == 415

    async def test_rejects_empty_file(self, api: tuple[httpx.AsyncClient, AsyncSession]) -> None:
        api_client, _ = api
        response = await post_upload(api_client, filename="empty.txt", content=b"")
        assert response.status_code == 415

    async def test_oversized_upload_is_413(
        self, app: FastAPI, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        base = get_settings()
        app.dependency_overrides[get_config] = lambda: base.model_copy(
            update={"upload_max_bytes": 10}
        )
        try:
            response = await post_upload(api_client, filename="big.txt", content=b"x" * 500)
        finally:
            app.dependency_overrides.pop(get_config, None)
        assert response.status_code == 413

    async def test_rejection_leaves_nothing_on_disk(
        self, api: tuple[httpx.AsyncClient, AsyncSession], upload_dir: Path
    ) -> None:
        """A failed upload must not leave bytes behind."""
        api_client, session = api
        before = await session.scalar(select(func.count()).select_from(Document))
        await post_upload(api_client, filename="virus.txt", content=EICAR_BYTES)
        after = await session.scalar(select(func.count()).select_from(Document))
        assert after == before

        stored = list(upload_dir.glob("*/*"))
        assert stored == [], f"unexpected stored files: {stored}"

    async def test_rejection_removes_staging_file(
        self, api: tuple[httpx.AsyncClient, AsyncSession], upload_dir: Path
    ) -> None:
        api_client, _ = api
        await post_upload(api_client, filename="virus.txt", content=EICAR_BYTES)
        staging = upload_dir / ".staging"
        assert not staging.exists() or not list(staging.iterdir())


class FakeEmbedderClient:
    """Stands in for the Nebius client so the real adapter path is exercised."""

    def __init__(self, vectors: list[list[float]] | None = None, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._vectors = vectors
        self._fail = fail

    async def create_embeddings(
        self, *, input_texts: list[str], model: str | None = None
    ) -> list[list[float]]:
        self.calls.append(list(input_texts))
        if self._fail:
            raise RuntimeError("provider unavailable")
        if self._vectors is not None:
            return self._vectors
        return [[0.1, 0.2, 0.3] for _ in input_texts]


def install_embedder(app: FastAPI, client: object) -> None:
    """Put a fake provider where the upload endpoint looks for one."""
    app.state.nebius_client = client


class TestEmbedding:
    async def test_embeds_chunks_when_provider_available(
        self, app: FastAPI, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        fake = FakeEmbedderClient()
        install_embedder(app, fake)
        api_client, session = api

        response = await post_upload(api_client, filename="notes.txt", content=TEXT.encode())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == DocumentStatus.READY.value
        assert body["embedded"] is True
        assert body["embedding_model"]
        assert fake.calls, "provider should have been called"

        document = await session.get(Document, uuid.UUID(body["id"]))
        assert document is not None
        chunks = (
            await session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document.id)
                .order_by(DocumentChunk.ordinal)
            )
        ).all()
        assert chunks
        assert all(chunk.embedding == [0.1, 0.2, 0.3] for chunk in chunks)

    async def test_embedding_input_is_sent(
        self, app: FastAPI, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        """The text actually vectorised is the chunk content."""
        fake = FakeEmbedderClient()
        install_embedder(app, fake)
        api_client, _ = api
        await post_upload(api_client, filename="notes.txt", content=TEXT.encode())
        assert any("First paragraph" in text for batch in fake.calls for text in batch)

    async def test_provider_failure_degrades_not_rejects(
        self, app: FastAPI, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        """Ingestion must still succeed when the embedding provider is down."""
        install_embedder(app, FakeEmbedderClient(fail=True))
        api_client, session = api

        response = await post_upload(api_client, filename="notes.txt", content=TEXT.encode())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["embedded"] is False
        assert any("embedding skipped" in w for w in body["warnings"])

        # Raw text is still stored, so the document can be re-embedded later.
        document = await session.get(Document, uuid.UUID(body["id"]))
        assert document is not None
        chunk = await session.scalar(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
        assert chunk is not None
        assert chunk.content
        assert chunk.embedding is None

    async def test_wrong_vector_count_degrades(
        self, app: FastAPI, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        """A provider returning too few vectors must not corrupt the mapping."""
        install_embedder(app, FakeEmbedderClient(vectors=[]))
        api_client, _ = api

        response = await post_upload(api_client, filename="notes.txt", content=TEXT.encode())
        assert response.status_code == 201, response.text
        assert response.json()["embedded"] is False

    async def test_upload_without_provider_still_succeeds(
        self, app: FastAPI, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        """No configured provider must not block ingestion."""
        app.state.nebius_client = None
        api_client, _ = api
        response = await post_upload(api_client, filename="notes.txt", content=TEXT.encode())
        assert response.status_code == 201, response.text
        assert response.json()["chunk_count"] >= 1


class TestRetrieval:
    async def test_get_document_with_chunks(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        created = (await post_upload(api_client, filename="a.txt", content=TEXT.encode())).json()
        response = await api_client.get(f"/api/v1/documents/{created['id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == created["id"]
        assert len(body["chunks"]) == created["chunk_count"]
        assert body["chunks"][0]["content"]

    async def test_list_chunks_returns_ordered(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        created = (await post_upload(api_client, filename="a.txt", content=TEXT.encode())).json()
        response = await api_client.get(f"/api/v1/documents/{created['id']}/chunks")
        assert response.status_code == 200
        chunks = response.json()
        assert [chunk["ordinal"] for chunk in chunks] == list(range(len(chunks)))

    async def test_list_documents(self, api: tuple[httpx.AsyncClient, AsyncSession]) -> None:
        api_client, _ = api
        await post_upload(api_client, filename="a.txt", content=TEXT.encode())
        await post_upload(api_client, filename="b.txt", content=TEXT.encode())
        response = await api_client.get("/api/v1/documents")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    async def test_chunk_offsets_are_exposed(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        created = (await post_upload(api_client, filename="a.txt", content=TEXT.encode())).json()
        chunks = (await api_client.get(f"/api/v1/documents/{created['id']}/chunks")).json()
        assert all(chunk["char_start"] is not None for chunk in chunks)
        assert all(chunk["char_end"] > chunk["char_start"] for chunk in chunks)

    async def test_delete_removes_record_and_file(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, session = api
        created = (await post_upload(api_client, filename="a.txt", content=TEXT.encode())).json()
        doc_id = uuid.UUID(created["id"])
        document = await session.get(Document, doc_id)
        assert document is not None
        stored = Path(document.storage_path)

        response = await api_client.delete(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 204
        assert await session.get(Document, doc_id) is None
        assert not stored.exists()

    async def test_delete_cascades_chunks(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, session = api
        created = (await post_upload(api_client, filename="a.txt", content=TEXT.encode())).json()
        doc_id = uuid.UUID(created["id"])
        await api_client.delete(f"/api/v1/documents/{doc_id}")
        remaining = await session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == doc_id)
        )
        assert remaining == 0


class TestOwnership:
    async def test_other_user_gets_404(self, api: tuple[httpx.AsyncClient, AsyncSession]) -> None:
        """A foreign document is reported as missing, not forbidden."""
        api_client, _ = api
        created = (
            await post_upload(
                api_client,
                filename="secret.txt",
                content=TEXT.encode(),
                email="alice@example.com",
            )
        ).json()

        response = await api_client.get(
            f"/api/v1/documents/{created['id']}",
            headers={"X-User-Email": "bob@example.com"},
        )
        assert response.status_code == 404

    async def test_other_user_cannot_delete(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        created = (
            await post_upload(
                api_client,
                filename="secret.txt",
                content=TEXT.encode(),
                email="alice@example.com",
            )
        ).json()
        response = await api_client.delete(
            f"/api/v1/documents/{created['id']}",
            headers={"X-User-Email": "bob@example.com"},
        )
        assert response.status_code == 404

    async def test_list_is_scoped_to_owner(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        await post_upload(
            api_client, filename="a.txt", content=TEXT.encode(), email="alice@example.com"
        )
        response = await api_client.get(
            "/api/v1/documents", headers={"X-User-Email": "bob@example.com"}
        )
        assert response.status_code == 200
        assert response.json()["total"] == 0

    async def test_unknown_id_is_404(self, api: tuple[httpx.AsyncClient, AsyncSession]) -> None:
        api_client, _ = api
        response = await api_client.get(f"/api/v1/documents/{uuid.uuid4()}")
        assert response.status_code == 404


class TestDevAuth:
    async def test_rejects_when_dev_auth_disabled(
        self, app: FastAPI, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        """With no auth backend configured, no identity is trusted at all."""
        api_client, _ = api
        base = get_settings()
        app.dependency_overrides[get_config] = lambda: base.model_copy(
            update={"dev_auth_enabled": False}
        )
        try:
            response = await api_client.get("/api/v1/documents")
        finally:
            app.dependency_overrides.pop(get_config, None)
        assert response.status_code == 401

    async def test_disabled_auth_blocks_upload_too(
        self, app: FastAPI, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        base = get_settings()
        app.dependency_overrides[get_config] = lambda: base.model_copy(
            update={"dev_auth_enabled": False}
        )
        try:
            response = await post_upload(api_client, filename="a.txt", content=TEXT.encode())
        finally:
            app.dependency_overrides.pop(get_config, None)
        assert response.status_code == 401

    async def test_rejects_malformed_email(
        self, api: tuple[httpx.AsyncClient, AsyncSession]
    ) -> None:
        api_client, _ = api
        response = await api_client.get(
            "/api/v1/documents", headers={"X-User-Email": "not-an-email"}
        )
        assert response.status_code == 400


class TestSchemaIntegration:
    """Schema-level assertions; no request or database session is needed."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_openapi_exposes_upload(self, app: FastAPI) -> None:
        paths = app.openapi()["paths"]
        assert "/api/v1/documents/upload" in paths
        assert "post" in paths["/api/v1/documents/upload"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_document_routes_present(self, app: FastAPI) -> None:
        paths = set(app.openapi()["paths"])
        assert {
            "/api/v1/documents",
            "/api/v1/documents/upload",
            "/api/v1/documents/{document_id}",
            "/api/v1/documents/{document_id}/chunks",
        } <= paths

    @pytest.mark.asyncio(loop_scope="function")
    async def test_document_tables_declared(self) -> None:
        assert {"documents", "document_chunks"} <= set(Base.metadata.tables)
