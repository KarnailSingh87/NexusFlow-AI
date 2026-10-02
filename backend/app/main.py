"""NexusFlow AI — FastAPI application entrypoint.

Bridges the Next.js UI to NVIDIA Nemotron models served by Nebius Token
Factory, and owns persistence plus request lifecycle concerns.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.endpoints import health
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, request_scope
from app.core.metrics import metrics
from app.db.session import dispose_engine, pool_status, warmup_pool
from app.services.nebius import NebiusClient, NebiusConfigurationError, NebiusError

configure_logging()
logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up / shut-down lifecycle.

    Opening the database pool here keeps the first user request off the
    connection handshake; the matching ``dispose_engine`` on the way out returns
    every pooled socket to the OS so a rolling restart does not leak them.
    """
    logger.info(
        "starting %s v%s (env=%s) base_url=%s default_model=%s",
        settings.app_name,
        settings.app_version,
        settings.app_env,
        settings.nebius_base_url,
        settings.nemotron_default_model,
    )

    # Returns False and logs a warning instead of raising unless DB_FAIL_FAST is
    # set; /health/ready probes the database live, so no boot-time flag is stored.
    await warmup_pool()

    nebius_client: NebiusClient | None = None
    try:
        nebius_client = NebiusClient(settings)
        # Force header construction so a missing key is reported at boot.
        _ = nebius_client.client
    except NebiusConfigurationError as exc:
        logger.error("inference disabled: %s", exc.message)

    app.state.nebius_client = nebius_client

    try:
        yield
    finally:
        if nebius_client is not None:
            await nebius_client.aclose()
        logger.info("database pool before shutdown: %s", pool_status())
        await dispose_engine()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    """Application factory (also used by tests)."""
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        summary="Control plane for NVIDIA Nemotron models on Nebius Token Factory.",
        description=(
            "NexusFlow AI exposes an OpenAI-compatible gateway over Nebius Token "
            "Factory, where NVIDIA Nemotron checkpoints are served serverlessly."
        ),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )

    # -- middleware --------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
        max_age=3600,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        """Correlate, time, and account for every inbound request.

        Also the single place the metrics registry learns about traffic: a request
        that dies before producing a response is still counted as a 5xx.
        """
        with request_scope(request.headers.get(REQUEST_ID_HEADER)) as request_id:
            request.state.request_id = request_id
            started = time.perf_counter()
            metrics.request_started()

            try:
                response = await call_next(request)
            except Exception:
                elapsed_ms = (time.perf_counter() - started) * 1000
                metrics.request_finished(
                    route=request.url.path, status_code=500, latency_ms=elapsed_ms
                )
                logger.exception(
                    "%s %s raised after %.1fms [request_id=%s]",
                    request.method,
                    request.url.path,
                    elapsed_ms,
                    request_id,
                )
                raise

            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"

            # Metric labels use the matched route template ("/documents/{id}") so a
            # hot document cannot explode metric cardinality; unmatched requests fall
            # back to the raw path. Logs keep the full path the client actually hit.
            route = request.scope.get("route")
            route_label = getattr(route, "path", None) or request.url.path
            metrics.request_finished(
                route=route_label, status_code=response.status_code, latency_ms=elapsed_ms
            )

            log = logger.warning if response.status_code >= 500 else logger.info
            log(
                "%s %s -> %s in %.1fms [request_id=%s]",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                request_id,
            )
            return response

    # -- error handlers ----------------------------------------------------
    @app.exception_handler(NebiusError)
    async def nebius_error_handler(request: Request, exc: NebiusError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.error("nebius error (%s): %s", exc.__class__.__name__, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.__class__.__name__,
                    "message": exc.message,
                    "detail": exc.detail,
                },
                "request_id": request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "ValidationError",
                    "message": "Request payload failed validation.",
                    "detail": exc.errors(),
                },
                "request_id": request_id,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "HTTPError",
                    "message": str(exc.detail),
                },
                "request_id": getattr(request.state, "request_id", None),
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled error: %s", exc.__class__.__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "InternalServerError",
                    "message": "An unexpected error occurred.",
                },
                "request_id": request_id,
            },
        )

    # -- routes ------------------------------------------------------------
    # Probes stay unversioned so container orchestrators have a stable target.
    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        target = "/docs" if settings.docs_enabled else f"{settings.api_v1_prefix}/health"
        return RedirectResponse(url=target)

    return app


app = create_app()
