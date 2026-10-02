# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# NexusFlow AI — backend image (Python 3.12 / FastAPI)
# Multi-stage so that the compiler toolchain used by any wheels never reaches
# the runtime layer.
# ---------------------------------------------------------------------------

# --- Stage 1: build the dependency virtualenv ---------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# --- Stage 2: runtime ----------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

# Non-root runtime user. UID is fixed so bind-mounted host files keep
# predictable ownership.
RUN groupadd --gid 1001 nexusflow \
 && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash nexusflow

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY --chown=nexusflow:nexusflow alembic.ini ./
COPY --chown=nexusflow:nexusflow alembic ./alembic
COPY --chown=nexusflow:nexusflow app ./app
COPY --chown=nexusflow:nexusflow docker-entrypoint.sh ./docker-entrypoint.sh

RUN chmod +x ./docker-entrypoint.sh && chown -R nexusflow:nexusflow /app

USER nexusflow

EXPOSE 8000

# Liveness only: /health never touches PostgreSQL or Nebius, so a slow
# dependency never causes Docker to restart a healthy process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
