# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# NexusFlow AI — frontend image (Next.js 16, App Router)
#
# Three stages:
#   deps    — install dependencies for layer caching
#   builder — produce the standalone server bundle
#   runner  — copy only the runtime artefacts
# ---------------------------------------------------------------------------

FROM node:22-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

FROM node:22-bookworm-slim AS builder
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* values are inlined into the client bundle at build time, so
# they must be present here rather than injected at container start.
ARG NEXT_PUBLIC_APP_NAME="NexusFlow AI"
ARG NEXT_PUBLIC_APP_ENV=production
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ARG NEXT_PUBLIC_ENABLE_STREAMING=true
ARG NEXT_PUBLIC_DEFAULT_MODEL=nvidia/Nemotron-3_5-Lightning
ARG NEXT_PUBLIC_API_TIMEOUT_MS=120000

ENV NEXT_PUBLIC_APP_NAME=$NEXT_PUBLIC_APP_NAME \
    NEXT_PUBLIC_APP_ENV=$NEXT_PUBLIC_APP_ENV \
    NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_ENABLE_STREAMING=$NEXT_PUBLIC_ENABLE_STREAMING \
    NEXT_PUBLIC_DEFAULT_MODEL=$NEXT_PUBLIC_DEFAULT_MODEL \
    NEXT_PUBLIC_API_TIMEOUT_MS=$NEXT_PUBLIC_API_TIMEOUT_MS

RUN npm run build

FROM node:22-bookworm-slim AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN groupadd --gid 1001 nodejs \
 && useradd --uid 1001 --gid 1001 --shell /bin/bash --create-home nextjs

# `output: 'standalone'` produces a self-contained server with a pruned
# node_modules — nothing from the build stage needs to ship.
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

CMD ["node", "server.js"]
