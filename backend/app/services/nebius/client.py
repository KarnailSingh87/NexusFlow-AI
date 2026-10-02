"""Async client for the Nebius Token Factory OpenAI-compatible API.

Token Factory exposes an OpenAI-shaped surface at
``https://api.tokenfactory.nebius.com/v1``, so this client deliberately
speaks plain JSON over HTTP instead of depending on a vendor SDK. That keeps
NexusFlow portable if the endpoint is ever swapped for a self-hosted,
OpenAI-compatible inference server.

Authentication is ``Authorization: Bearer $NEBIUS_API_KEY``. The key is only
ever held in memory and attached to request headers — never logged, never
persisted.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Sequence
from types import TracebackType
from typing import Any, Self

import httpx

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class NebiusError(Exception):
    """Base class for every Token Factory failure surfaced by NexusFlow."""

    status_code: int = 502

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NebiusConfigurationError(NebiusError):
    """Raised when the client is not configured (e.g. missing API key)."""

    status_code = 503


class NebiusAuthError(NebiusError):
    """Raised on 401/403 — invalid, revoked, or out-of-quota API key."""

    status_code = 401


class NebiusNotFoundError(NebiusError):
    """Raised on 404 — unknown model or endpoint."""

    status_code = 404


class NebiusRateLimitError(NebiusError):
    """Raised on 429 after retries have been exhausted."""

    status_code = 429

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NebiusUpstreamError(NebiusError):
    """Raised on 5xx or unparseable upstream responses."""

    status_code = 502


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class NebiusClient:
    """Thin async wrapper around the Token Factory inference endpoints."""

    def __init__(
        self,
        config: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = config or settings
        self._owns_client = client is None
        self._client = client

    # -- lifecycle ---------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        """Return the lazily-constructed shared HTTP client."""
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.nebius_base_url,
            headers=self._headers(),
            timeout=httpx.Timeout(self._settings.nebius_timeout_seconds),
            verify=self._settings.nebius_verify_tls,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )

    def _headers(self) -> dict[str, str]:
        key = self._settings.nebius_api_key.get_secret_value().strip()
        if not key:
            raise NebiusConfigurationError(
                "NEBIUS_API_KEY is not configured. "
                "Copy backend/.env.example to backend/.env and set your key "
                "(https://tokenfactory.nebius.com/project/api-keys)."
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{self._settings.app_slug}/{self._settings.app_version}",
        }

    async def __aenter__(self) -> Self:
        _ = self.client  # force construction so config errors surface early
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- request plumbing --------------------------------------------------
    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform a request with exponential backoff on transient failures."""
        attempts = self._settings.nebius_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.request(
                    method, url, json=json_body, headers=self._headers()
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                logger.warning(
                    "Nebius request timed out (attempt %d/%d): %s", attempt, attempts, url
                )
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("Nebius transport error (attempt %d/%d): %s", attempt, attempts, exc)
            else:
                if response.status_code < 400:
                    return response
                if response.status_code in (401, 403):
                    raise NebiusAuthError(
                        "Nebius Token Factory rejected the API key "
                        f"(HTTP {response.status_code}). Check NEBIUS_API_KEY.",
                        detail=self._safe_body(response),
                    )
                if response.status_code == 404:
                    raise NebiusNotFoundError(
                        "Nebius resource not found. Verify the model ID and that "
                        "the model is available in your region.",
                        detail=self._safe_body(response),
                    )
                if response.status_code == 429:
                    if attempt < attempts:
                        delay = self._retry_delay(response, attempt)
                        logger.warning(
                            "Nebius rate limited; retrying in %.2fs (attempt %d/%d)",
                            delay,
                            attempt,
                            attempts,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise NebiusRateLimitError(
                        "Nebius rate limit exceeded. Reduce request concurrency "
                        "or upgrade your Token Factory quota.",
                        retry_after=self._parse_retry_after(response),
                    )
                if response.status_code >= 500 and attempt < attempts:
                    delay = self._retry_delay(response, attempt)
                    logger.warning(
                        "Nebius upstream %d; retrying in %.2fs (attempt %d/%d)",
                        response.status_code,
                        delay,
                        attempt,
                        attempts,
                    )
                    await asyncio.sleep(delay)
                    continue

                raise NebiusUpstreamError(
                    f"Nebius Token Factory returned HTTP {response.status_code}",
                    detail=self._safe_body(response),
                )

            if attempt < attempts:
                await asyncio.sleep(self._retry_delay(None, attempt))

        raise NebiusUpstreamError(
            f"Nebius Token Factory request failed after {attempts} attempts: {last_error}"
        )

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        """Exponential backoff with jitter, honouring ``Retry-After``."""
        if response is not None:
            hinted = NebiusClient._parse_retry_after(response)
            if hinted is not None:
                return min(hinted, 30.0)
        backoff = min(2.0 ** (attempt - 1), 20.0)
        return backoff + random.uniform(0, backoff * 0.25)  # noqa: S311 - not crypto

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    @staticmethod
    def _safe_body(response: httpx.Response) -> Any:
        """Best-effort JSON error body, truncated for log safety."""
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return response.text[:500]

    def _model_is_allowed(self, model: str) -> bool:
        allowlist = [m.strip() for m in self._settings.model_allowlist if m.strip()]
        return not allowlist or model in allowlist

    def _validate_model(self, model: str | None) -> str:
        resolved = (model or self._settings.nemotron_default_model or "").strip()
        if not resolved:
            raise NebiusConfigurationError(
                "No model requested and NEMOTRON_DEFAULT_MODEL is unset."
            )
        if not self._model_is_allowed(resolved):
            raise NebiusNotFoundError(
                f"Model '{resolved}' is not in MODEL_ALLOWLIST.",
                detail={"allowlist": self._settings.model_allowlist},
            )
        return resolved

    def _validate_generation_params(
        self, temperature: float | None, max_tokens: int | None
    ) -> tuple[float, int]:
        default_temp = self._settings.llm_default_temperature
        default_max = self._settings.llm_default_max_tokens
        temp = default_temp if temperature is None else float(temperature)
        tokens = default_max if max_tokens is None else int(max_tokens)

        if not 0.0 <= temp <= 2.0:
            raise ValueError("temperature must be between 0.0 and 2.0")
        if tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if tokens > self._settings.llm_max_max_tokens:
            tokens = self._settings.llm_max_max_tokens
        return temp, tokens

    # -- public API --------------------------------------------------------
    async def list_models(self) -> list[dict[str, Any]]:
        """Return the models the API key can access (OpenAI ``/models`` shape)."""
        response = await self._request("GET", "/models")
        payload = response.json()
        models = payload.get("data", []) if isinstance(payload, dict) else []
        return [m for m in models if isinstance(m, dict)]

    async def create_chat_completion(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: Sequence[str] | None = None,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Non-streaming chat completion. Returns the raw upstream JSON body."""
        resolved_model = self._validate_model(model)
        temp, tokens = self._validate_generation_params(temperature, max_tokens)

        body: dict[str, Any] = {
            "model": resolved_model,
            "messages": list(messages),
            "temperature": temp,
            "max_tokens": tokens,
            "stream": stream,
        }
        if top_p is not None:
            body["top_p"] = top_p
        if stop:
            body["stop"] = list(stop)
        if tools:
            body["tools"] = tools
        if response_format:
            body["response_format"] = response_format
        if extra_body:
            body.update(extra_body)

        response = await self._request("POST", "/chat/completions", json_body=body)
        return response.json()

    async def stream_chat_completion(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: Sequence[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Yield raw SSE lines from ``/chat/completions`` with ``stream=true``.

        Raw lines are yielded rather than decoded deltas so that consumers
        (the Next.js client) stay wire-compatible with the OpenAI protocol and
        can forward events verbatim.
        """
        resolved_model = self._validate_model(model)
        temp, tokens = self._validate_generation_params(temperature, max_tokens)

        body: dict[str, Any] = {
            "model": resolved_model,
            "messages": list(messages),
            "temperature": temp,
            "max_tokens": tokens,
            "stream": True,
        }
        if top_p is not None:
            body["top_p"] = top_p
        if stop:
            body["stop"] = list(stop)
        if tools:
            body["tools"] = tools
        if extra_body:
            body.update(extra_body)

        try:
            async with self.client.stream(
                "POST", "/chat/completions", json=body, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if line:
                        yield line
        except httpx.TimeoutException as exc:
            raise NebiusUpstreamError("Nebius streaming request timed out.") from exc
        except httpx.HTTPError as exc:
            raise NebiusUpstreamError(f"Nebius streaming request failed: {exc}") from exc

    async def create_embeddings(
        self,
        *,
        input_texts: Sequence[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Embed texts. Defaults to the configured embedding model."""
        resolved_model = (model or self._settings.nemotron_embedding_model).strip()
        response = await self._request(
            "POST",
            "/embeddings",
            json_body={"model": resolved_model, "input": list(input_texts)},
        )
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        # `index` is not guaranteed to be ordered by the upstream provider.
        return [item["embedding"] for item in sorted(data, key=lambda d: d.get("index", 0))]

    async def ping(self) -> bool:
        """Cheap connectivity probe used by the readiness endpoint."""
        try:
            await self.list_models()
        except NebiusError:
            return False
        return True

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Translate an error response into the matching Nebius exception."""
        detail = NebiusClient._safe_body(response)
        status = response.status_code
        if status in (401, 403):
            raise NebiusAuthError("Nebius rejected the API key.", detail=detail)
        if status == 404:
            raise NebiusNotFoundError("Nebius resource not found.", detail=detail)
        if status == 429:
            raise NebiusRateLimitError(
                "Nebius rate limit exceeded.",
                retry_after=NebiusClient._parse_retry_after(response),
            )
        raise NebiusUpstreamError(f"Nebius returned HTTP {status}", detail=detail)
