# NexusFlow AI

> ## ⚠️ License Notice — Please Read First
>
> **NexusFlow AI is licensed under the Apache License, Version 2.0.**
>
> The complete license text is in [`LICENSE`](./LICENSE). In short: you may use,
> modify, and distribute this software — including commercially — provided you
> keep the copyright notice and state any changes, and you provide the NOTICE
> file contents if one is distributed upstream.
>
> ```
> Copyright 2026 The NexusFlow AI Contributors
> Licensed under the Apache License, Version 2.0 (the "License");
> you may not use this file except in compliance with the License.
> You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
> Unless required by applicable law or agreed to in writing, software
> distributed under the License is distributed on an "AS IS" BASIS,
> WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
> See the License for the specific language governing permissions and
> limitations under the License.
> ```
>
> **This project is not affiliated with, endorsed by, or sponsored by NVIDIA,
> Nebius, or any of their subsidiaries.** "Nemotron" and "Token Factory" are
> trademarks of their respective owners, used here only to describe the
> third-party services this software integrates with. All model weights remain
> subject to their own licenses — see the
> [NVIDIA Open Model License](https://build.nvidia.com/open-model-license) and
> the [Nebius terms of service](https://nebius.com/terms).
>
> **You are responsible for your own usage.** Running inference costs money
> (per-token billing via Nebius) and your prompts flow to a third-party
> provider. Review your data-handling obligations before sending anything
> sensitive.

---

## Executive Summary

**NexusFlow AI is an open-source control plane for building AI agent workflows on
NVIDIA Nemotron models, served serverlessly by Nebius Token Factory.**

NVIDIA publishes the Nemotron family of open-weight models — hybrid
mixture-of-experts checkpoints spanning 12B to 550B parameters, tuned for
reasoning, tool use and long-horizon agentic work. Nebius Token Factory hosts
them behind an OpenAI-compatible HTTP API with per-token billing, autoscaling
and no GPU management. What is missing is the connective tissue: the auth
boundary, retry and quota handling, cost ledger, streaming plumbing and
deployment story that turns a raw completion endpoint into a product.

NexusFlow AI supplies that connective tissue as a two-service monorepo.

| | |
|---|---|
| **Frontend** | Next.js 16 (App Router) + React 19 + TypeScript + Tailwind CSS v4. Server Components for dashboards, client components for the streaming playground. |
| **Backend** | Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2.0 (async) + Alembic. Owns the Nebius credential, enforces quotas, records token spend. |
| **Provider** | Nebius Token Factory (`https://api.tokenfactory.nebius.com/v1`) — OpenAI-compatible `/models`, `/chat/completions`, `/embeddings`. |
| **Models** | NVIDIA Nemotron 3.5 Lightning, Nemotron 3 Super, Nemotron 3 Ultra, Nemotron Nano V2 (vision). |
| **Storage** | PostgreSQL 17 — workflows, execution ledger, per-run token and cost accounting. |
| **Orchestration** | `docker compose up --build` runs the whole stack: database, migrations, API and UI. |

### Why this design

- **The provider key never reaches the browser.** Only the FastAPI process holds
  `NEBIUS_API_KEY`. One chokepoint means one place to rate-limit, retry,
  redact, audit and bill — and no key material to leak from client-side
  JavaScript.
- **OpenAI wire compatibility throughout.** Requests, responses and SSE frames
  match the OpenAI schema. Swapping Token Factory for a self-hosted vLLM server
  is a base-URL change, not a rewrite.
- **Degrades instead of failing.** If Token Factory is unreachable, the model
  catalogue falls back to a curated local registry so the UI still renders.
- **Correctness is enforced early.** Oversized prompts are rejected before
  they are billed; unknown models are rejected before they reach the provider;
  `APP_ENV=production` refuses to boot with a placeholder secret.
- **Defaults are opinionated and swappable.** `nvidia/Nemotron-3_5-Lightning`
  (~1M context, ~$0.06/1M input) is the default because it is fast and cheap;
  the 120B Super and 550B Ultra models are one env var away.

### Model lineup

Snapshot pricing (USD per 1M tokens), accurate as of October 2026. Verify in the
[Token Factory console](https://tokenfactory.nebius.com/) before relying on it
for billing.

| Model ID | Tier | Params | Context | In / 1M | Out / 1M |
|---|---|---|---|---|---|
| `nvidia/Nemotron-3_5-Lightning` | fast | 30B MoE (3B active) | 1M | $0.06 | $0.24 |
| `nvidia/nemotron-3-super-120b-a12b` | balanced | 120B MoE (12B active) | 256K | $0.30 | $0.90 |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | frontier | 550B MoE (55B active) | 1M | $1.00 | $3.00 |
| `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | fast | 30B MoE | 256K | $0.06 | $0.24 |
| `nvidia/Nemotron-Nano-V2-12b` | vision | 12B VL | 128K | $0.06 | $0.24 |
| `Qwen/Qwen3-Embedding-8B` | embedding | 8B | 41K | $0.01 | — |

> **Deprecation note.** `nvidia/Llama-3_1-Nemotron-Ultra-253B-v1` and
> `nvidia/Nemotron-3-Nano-Omni` were withdrawn from Token Factory serverless on
> **2026-08-31**. The API flags them as deprecated and returns a recommended
> replacement rather than a bare 404. Check the
> [deprecation notices](https://docs.tokenfactory.nebius.com/deprecations)
> before pinning a model ID.

---

## Screenshots

<!-- Replace these placeholders with real captures before submission. -->

| View | Capture |
|---|---|
| Architecture overview | ![Architecture](docs/screenshots/architecture.png) |
| Streaming playground | ![Playground](docs/screenshots/playground.png) |
| Agentic copilot trace | ![Copilot](docs/screenshots/copilot.png) |
| Dashboard & jobs | ![Dashboard](docs/screenshots/dashboard.png) |

## Architecture

The full request path from browser to GPU, and back.

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  BROWSER (user)                                                          │
  │                                                                          │
  │  NexusFlow AI UI — Next.js 16 App Router · React 19                      │
  │  Playground · model picker · SSE stream · token usage                    │
  └────────────────────────────────┬─────────────────────────────────────────┘
                                   │  HTTPS · fetch() · EventSource
                                   │  Origin: http://localhost:3000
                                   │  CORS preflight (CORS_ORIGINS allowlist)
                                   ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  FastAPI CONTROL PLANE  ·  :8000                                         │
  │                                                                          │
  │app/main.py                                                               │
  │  ├── request-id middleware + redacting logger                            │
  │  ├── CORS, uniform errors → { error, request_id }                        │
  │  ├── lifespan: pool warm-up on boot · dispose on exit                    │
  │  └── request lifecycle + token counters                                  │
  │                                                                          │
  │GET  /health                      liveness (no I/O)                       │
  │GET  /health/ready                DB + provider probe                     │
  │GET  /api/v1/models               live ∪ curated registry                 │
  │                                                                          │
  │POST /api/v1/chat/completions     buffered completion                     │
  │POST /api/v1/chat/completions/stream   SSE passthrough                    │
  │POST /api/v1/chat/embeddings      vector embeddings                       │
  │                                                                          │
  │POST /api/v1/documents/upload     PDF · DOCX · TXT · CSV                  │
  │GET  /api/v1/documents/{id}       metadata + chunks                       │
  │                                                                          │
  │app/services/nebius/client.py                                             │
  │  Bearer auth · jittered retry · 429/5xx backoff                          │
  │  key never logged                                                        │
  │                                                                          │
  │app/services/ai_service.py                                                │
  │  task → Nemotron route (Nano · Lightning · Super · Ultra)                │
  │  JSON mode: request + verify + repair                                    │
  │                                                                          │
  │app/services/documents/                                                   │
  │  magic-byte validation · EICAR + optional ClamAV                         │
  │  extract → semantic chunk → embed → store                                │
  │                                                                          │
  │               ┌──────────────────────────────────────────────────────┐   │
  │               │  PostgreSQL 17   SQLAlchemy 2.0 async · asyncpg      │   │
  │               │  runs · steps · documents · jobs · Alembic           │   │
  │               └──────────────────────────────────────────────────────┘   │
  └────────────────────────────────┬─────────────────────────────────────────┘
                                   │  HTTPS · Bearer $NEBIUS_API_KEY
                                   │  GET  {NEBIUS_BASE_URL}/models
                                   │  POST {NEBIUS_BASE_URL}/chat/completions
                                   │  POST {NEBIUS_BASE_URL}/embeddings
                                   ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  NEBIUS  TOKEN  FACTORY  ·  serverless inference                         │
  │  https://api.tokenfactory.nebius.com/v1                                  │
  │                                                                          │
  │  OpenAI-compatible: /models · /chat/completions                          │
  │  /embeddings · SSE streaming · quotas · rate limits                      │
  │                                                                          │
  │               ┌──────────────────────────────────────────────────────┐   │
  │               │  NVIDIA  NEMOTRON  —  open-weight checkpoints        │   │
  │               │                                                      │   │
  │               │  Nemotron 3.5 Lightning  30B MoE   1M ctx    fast    │   │
  │               │  Nemotron 3 Super        120B MoE  256K ctx  balanced│   │
  │               │  Nemotron 3 Ultra        550B MoE  1M ctx    frontier│   │
  │               │  Nemotron Nano V2        12B VL    128K ctx  vision  │   │
  │               │                                                      │   │
  │               │  H100 / H200 / B200 · vLLM · FP8 / NVFP4             │   │
  │               └──────────────────────────────────────────────────────┘   │
  └──────────────────────────────────────────────────────────────────────────┘
```

### Trust boundaries

```
  TRUSTED                          SEMI-TRUSTED                        EXTERNAL
  ───────                          ─────────────                       ────────
  Browser (untrusted input)        FastAPI process                    Nebius edge
  Next.js server (holds no        holds NEBIUS_API_KEY               Third-party
  provider key)                   validates every request            infrastructure
                                  redacts every log line
```

Three rules keep this boundary intact:

1. **`NEBIUS_API_KEY` is backend-only.** It has no `NEXT_PUBLIC_` prefix, and
   `backend/.env` is git-ignored. It is read once, held in memory, attached as a
   bearer header, and masked by `app/core/logging.py` if it ever reaches a log.
2. **`NEXT_PUBLIC_*` is public.** Those variables are compiled into the browser
   bundle at build time. Only non-sensitive values (URLs, model IDs, UI flags)
   may use the prefix.
3. **`CORS_ORIGINS` is an allowlist, not a wildcard.** The browser calls the API
   directly, so the UI origin must be listed explicitly; `APP_ENV=production`
   refuses to start if `*` is configured.

### Request lifecycle

```
POST /api/v1/chat/completions/stream
  │
  ├─▶ middleware          attach X-Request-ID, start timer
  ├─▶ CORS preflight      origin ∈ CORS_ORIGINS ?
  ├─▶ Pydantic validate   role enum, temperature ≤ 2, prompt token estimate
  ├─▶ get_nebius_client   shared pool from app.state (503 if key unset)
  ├─▶ model allowlist     reject unknown IDs before spending money
  ├─▶ NebiusClient        POST {NEBIUS_BASE_URL}/chat/completions
  │     └─ retry loop     429 → honour Retry-After · 5xx/timeout → jittered backoff
  └─▶ StreamingResponse   forward `data: {...}` frames verbatim, then [DONE]
                            (mid-stream failure → terminal nexusflow_error frame)
```

---

## Repository Layout

```
nexusflow/
├── README.md                  ← you are here
├── LICENSE                    Apache 2.0
├── .env.example               ← Docker Compose configuration
├── docker-compose.yml         ← db + backend + frontend, one command
├── .editorconfig
├── .gitignore
│
├── backend/                   ← FastAPI control plane
│   ├── Dockerfile             multi-stage, non-root, HEALTHCHECK
│   ├── docker-entrypoint.sh   waits for DB, runs `alembic upgrade head`
│   ├── requirements.txt       pinned runtime deps
│   ├── requirements-dev.txt   pytest / ruff / mypy
│   ├── pyproject.toml         ruff + pytest + mypy config
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py             async engine, DATABASE_URL from settings
│   │   └── versions/          0001_initial · 0002_documents_jobs
│   ├── .env.example
│   └── app/
│       ├── main.py            app factory, middleware, exception handlers
│       ├── core/
│       │   ├── config.py      pydantic-settings + production safety rails
│       │   ├── logging.py     text/JSON logging with secret redaction
│       │   └── metrics.py     request lifecycle + token consumption counters
│       ├── db/
│       │   ├── base.py        declarative base, UUID + timestamp mixins
│       │   ├── session.py     async engine, pool warm-up, session dependency
│       │   └── models.py      User · UserSession · Document · DocumentChunk
│       │                      Workflow · WorkflowRun · WorkflowStep · BackgroundJob
│       ├── schemas/           request/response contracts
│       ├── services/
│       │   ├── ai_service.py   task → Nemotron model routing, JSON-mode contract
│       │   ├── documents/      ingestion engine
│       │   │   ├── validation.py  magic-byte sniffing, filename sanitisation
│       │   │   ├── scanning.py    EICAR + optional ClamAV, INSTREAM protocol
│       │   │   ├── extractors.py  PDF · DOCX · TXT · CSV text extraction
│       │   │   ├── chunking.py    semantic chunking with overlap + offsets
│       │   │   └── pipeline.py    stage → validate → screen → chunk → embed
│       │   └── nebius/
│       │       ├── client.py  async Token Factory client (retry, SSE, embed)
│       │       └── registry.py curated Nemotron catalogue
│       └── api/
│           ├── deps.py        DI wiring (settings, session, provider, principal)
│           └── v1/endpoints/  health · models · chat · documents
│   └── tests/                 pytest suite; provider traffic is mocked, and the
│                              ingestion tests use a real in-memory SQLite DB
│
└── frontend/                  ← Next.js 16 App Router
    ├── Dockerfile             3-stage standalone build
    ├── package.json
    ├── next.config.ts         output: 'standalone'
    ├── tsconfig.json          strict + noUncheckedIndexedAccess
    ├── eslint.config.mjs      flat config on eslint-config-next 16
    ├── postcss.config.mjs     @tailwindcss/postcss
    ├── .env.example
    ├── public/                favicon, robots.txt
    └── src/
        ├── app/
        │   ├── layout.tsx     shell, header, footer
        │   ├── globals.css    Tailwind v4 @theme tokens
        │   ├── page.tsx       dashboard (Server Component, live model list)
        │   ├── playground/    streaming chat
        │   └── architecture/  the diagram above, in-app
        ├── components/
        │   └── playground/Playground.tsx   ('use client', SSE reader)
        └── lib/
            ├── env.ts         typed env accessors
            ├── types.ts       mirrors backend schemas
            ├── api.ts         fetch wrapper + SSE parser
            └── server-api.ts  server-only probes (guards on 'server-only')
```

---

## Local Setup

### Prerequisites

| Tool | Version | Required for |
|---|---|---|
| [Docker](https://docs.docker.com/get-docker/) + Compose v2 | any recent | Option A (recommended) |
| Python | 3.11+ (3.12 recommended) | Option B |
| Node.js | 20.9+ (22 recommended) | Option B |
| Nebius account | — | All options |

**Get your Nebius API key:** <https://tokenfactory.nebius.com/project/api-keys>
→ *Create API key* → copy the value. **It is displayed exactly once.** If you
lose it, delete it and create another.

---

### Option A — Docker Compose (recommended)

One command brings up PostgreSQL, applies migrations, starts the API and serves
the UI.

```bash
# 1. Configure
cp .env.example .env

# 2. Add your credentials to .env
#    NEBIUS_API_KEY=nb_...
#    SECRET_KEY=...   (generate: python -c "import secrets; print(secrets.token_urlsafe(48))")

# 3. Launch
docker compose up --build
```

Compose **refuses to start** if `NEBIUS_API_KEY` or `SECRET_KEY` is empty, with
an actionable error rather than a broken stack.

| Service | URL |
|---|---|
| NexusFlow UI | <http://localhost:3000> |
| FastAPI docs (Swagger) | <http://localhost:8000/docs> |
| FastAPI health | <http://localhost:8000/health> |
| PostgreSQL | `localhost:5432` (`nexusflow` / `nexusflow`) |

Expected startup sequence:

```
nexusflow-db         healthy          (pg_isready passes)
nexusflow-backend    starting → healthy   (migrations applied, then uvicorn)
nexusflow-frontend   starting → healthy
```

**Verify it works:**

```bash
curl -s http://localhost:8000/health
# {"status":"ok","version":"0.1.0","environment":"development","checks":{"service":"up"}}

open http://localhost:3000        # the dashboard lists live Nemotron models
```

**Common operations:**

```bash
docker compose up -d --build      # start detached
docker compose logs -f backend    # tail API logs
docker compose ps                 # health status per service
docker compose restart backend    # restart one service
docker compose down               # stop, keep data
docker compose down -v            # stop, DELETE the database volume
docker compose exec backend \
  alembic revision --autogenerate -m "add prompt_templates table"
docker compose exec backend \
  python -m pytest -q             # run the test suite inside the container
```

> **`NEXT_PUBLIC_*` changes need a rebuild.** Those variables are inlined into
> the client bundle at build time, not injected at container start:
> `docker compose build --no-cache frontend && docker compose up -d`

---

### Option B — Run natively on your host

Use this when you want hot reload from your editor. Start PostgreSQL however you
prefer (Docker is easiest), then run each service in its own terminal.

**Step 1 — Database**

```bash
docker run -d --name nexusflow-db \
  -e POSTGRES_USER=nexusflow \
  -e POSTGRES_PASSWORD=nexusflow \
  -e POSTGRES_DB=nexusflow \
  -p 5432:5432 \
  postgres:17-alpine
```

**Step 2 — Backend**

```bash
cd backend

python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
```

Edit `backend/.env` and set at least:

```dotenv
NEBIUS_API_KEY=nb_your_real_key_here
SECRET_KEY=paste_a_long_random_string_here
DATABASE_URL=postgresql+asyncpg://nexusflow:nexusflow@localhost:5432/nexusflow
```

Apply migrations and start the API:

```bash
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

API is at <http://localhost:8000> · docs at <http://localhost:8000/docs>

**Step 3 — Frontend**

```bash
cd frontend
npm install
cp .env.example .env.local          # Next.js reads .env.local, not .env
```

Edit `frontend/.env.local`:

```dotenv
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
INTERNAL_API_BASE_URL=http://localhost:8000
```

Then:

```bash
npm run dev                         # http://localhost:3000
```

> If the browser reports a CORS error, add the exact frontend origin to
> `CORS_ORIGINS` in `backend/.env` and restart uvicorn. Matching scheme, host
> and port exactly — `localhost:3000` and `127.0.0.1:3000` are different origins.

---

## Configuration

Three environment templates exist, one per context:

| File | Used by | When |
|---|---|---|
| `.env.example` | `docker-compose.yml` | Docker Compose reads the root `.env` |
| `backend/.env.example` | `pydantic-settings` | Running uvicorn on your host |
| `frontend/.env.example` | Next.js (as `.env.local`) | Running `next dev` on your host |

### Essentials

| Variable | Where | Default | Purpose |
|---|---|---|---|
| `NEBIUS_API_KEY` | backend | *(required)* | Token Factory credential. Backend-only, never committed. |
| `NEBIUS_BASE_URL` | backend | `https://api.tokenfactory.nebius.com/v1` | OpenAI-compatible endpoint; `/v1` is enforced. |
| `DATABASE_URL` | backend | built from `POSTGRES_*` | Async SQLAlchemy DSN; `postgresql://` is upgraded to `postgresql+asyncpg://`. |
| `SECRET_KEY` | backend | placeholder | Signs tokens. Must be ≥32 chars in production. |
| `CORS_ORIGINS` | backend | `localhost:3000` | Browser origins allowed to call the API. |
| `NEMOTRON_DEFAULT_MODEL` | backend | `nvidia/Nemotron-3_5-Lightning` | Model used when a request omits `model`. |
| `NEMOTRON_NANO_MODEL` | backend | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Cheap route for summaries, entity extraction, and classification. |
| `MODEL_ALLOWLIST` | backend | *(empty)* | Restrict invokable model IDs. Empty = anything your key reaches. |
| `NEXT_PUBLIC_API_BASE_URL` | frontend | `http://localhost:8000` | API origin **as the browser sees it**. |
| `INTERNAL_API_BASE_URL` | frontend | `http://localhost:8000` | API origin **as the Next.js server sees it** (`http://backend:8000` in Compose). |
| `APP_SECRET` | frontend | *(empty)* | UI session signing key. |

Every variable is documented inline in the `.env.example` files — start there.

### Connection pool and logging

| Variable | Default | Purpose |
|---|---|---|
| `DB_POOL_RECYCLE_SECONDS` | `1800` | Recycle a pooled connection after this age, staying under the idle timeout imposed by pgbouncer and managed-Postgres proxies. |
| `DB_POOL_PRE_PING` | `true` | Validate a connection before handing it out, so a database restart cannot surface as a failed request. |
| `DB_CONNECT_RETRIES` | `5` | Attempts made during start-up pool warm-up. |
| `DB_CONNECT_BACKOFF_SECONDS` | `1.0` | Base delay between warm-up attempts; multiplied by the attempt number. |
| `DB_FAIL_FAST` | `false` | `false` lets the API boot and report itself degraded on `/health/ready`; `true` raises at start-up and lets the orchestrator retry. |
| `LOG_FORMAT` | `text` | `text` for a terminal, `json` for one JSON object per line in an aggregator. Both formats redact credentials. |

Pool settings only reach the container through `docker-compose.yml`, which
injects an explicit variable list rather than mounting `.env`. The Compose
block and `Settings` are covered by tests so a new knob cannot be added to one
and silently dropped from the other.

### Document ingestion

| Variable | Default | Purpose |
|---|---|---|
| `UPLOAD_MAX_BYTES` | `26214400` | Hard ceiling per upload (25 MiB). Enforced *while streaming*, so an oversized body is refused after one block instead of after 25 MiB of disk writes. |
| `UPLOAD_STORAGE_DIR` | platform temp dir | Where original bytes are written. Override in production; the container default is the `uploads` volume at `/data/uploads`. |
| `UPLOAD_ALLOWED_EXTENSIONS` | `pdf,docx,txt,csv` | Extensions this deployment accepts. Only a fast rejection path — the real gate is magic-byte sniffing. |
| `UPLOAD_REQUIRE_TEXT` | `true` | Reject uploads that yield no extractable text, such as scanned images or a header-only CSV. |
| `INGEST_CHUNK_SIZE` | `1200` | Target characters per chunk (~300 tokens). |
| `INGEST_CHUNK_OVERLAP` | `200` | Characters of trailing context repeated into the next chunk, so a fact spanning a boundary stays retrievable from both sides. |
| `INGEST_MAX_CHUNKS` | `2000` | Cap on chunks per document, bounding embedding spend on a single upload. Truncation is recorded in the final chunk's metadata, never silent. |
| `INGEST_EMBED_CHUNKS` | `true` | Vectorise during ingestion. With this off, chunks are stored as raw text only and uploads keep working while the embedding provider is down. |
| `CLAMAV_HOST` / `CLAMAV_PORT` | `""` / `3310` | Optional ClamAV daemon. Empty enables only the built-in EICAR check. |

### Access control (development only)

| Variable | Default | Purpose |
|---|---|---|
| `DEV_AUTH_ENABLED` | `true` | Enables the `X-User-Email` development principal. **This is not authentication.** |
| `DEV_USER_EMAIL` | `dev@nexusflow.local` | Identity used when a request carries no `X-User-Email` header. |

### Production safety rails

Setting `APP_ENV=production` makes `backend/app/core/config.py` **refuse to
boot** unless:

- `SECRET_KEY` is not a known placeholder and is at least 32 characters,
- `NEBIUS_API_KEY` is non-empty,
- `CORS_ORIGINS` is not `*`.

The failure is explicit, listing every problem at once, rather than a
cryptic runtime error later.

---

## Model Routing

`app/services/ai_service.py` decides *which* NVIDIA Nemotron model answers a
request, so call sites never hard-code a model ID. It sits above
`NebiusClient`, which already owns the pinned `/v1` base URL, `Bearer` auth,
backoff and `Retry-After` handling — re-implementing any of that per call site
would produce a second, divergent retry policy.

| Task | Route | Why |
|---|---|---|
| `summarize`, `extract_entities`, `classify` | Nemotron 3 **Nano** | Cheap, high-volume, schema-bound; a reasoning tier adds cost for no gain. |
| `draft` | Nemotron 3.5 **Lightning** | Interactive latency over depth. |
| `code`, `reason` | Nemotron 3 **Super** | Multi-step reasoning at moderate cost. |
| `deep_audit` | Nemotron 3 **Ultra** | Explicitly the hard case; reliability over cost. |
| `embed` | Qwen3 **Embedding 8B** | Dedicated encoder, not a chat model. |
| `chat` | configured default | No routing opinion applied. |

```python
from app.services.ai_service import get_nemotron_client

# Routes to Ultra, returns a parsed object, and records token usage.
audit = await get_nemotron_client("deep_audit").deep_audit(payment_service)
```

Task names are matched case- and separator-insensitively, so `"deep_audit"`,
`"DEEP-AUDIT"` and `"Deep Audit"` all resolve. An unrecognised task raises
`UnknownTaskError` rather than silently promoting the request to a frontier
model. Every route carries a `rationale`, and `explain_routing()` returns the
whole table with resolved model IDs for diagnostics.

### Structured output

`chat_json()` requests `response_format={"type": "json_object"}` **and then
verifies the reply parses**. That distinction matters: Nemotron 3 Nano and Ultra
are reasoning models, so they may emit a reasoning trace, wrap the object in a
```json fence, or precede it with prose. The extractor handles all three; if
parsing still fails the bad output is fed back as a correction and the call is
retried once before raising `StructuredOutputError`.

---

## API Reference

Base URL `http://localhost:8000`. Interactive docs at `/docs`; machine-readable
schema at `/openapi.json`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness. Touches nothing. Used by the container healthcheck. |
| `GET` | `/health/ready` | Readiness. Probes PostgreSQL and Token Factory. `503` if the DB is down. |
| `GET` | `/api/v1/models` | Model inventory. `?provider=live\|curated\|auto`, `?nemotron_only=true`. |
| `GET` | `/api/v1/models/catalog` | Curated Nemotron cards. No provider call. |
| `GET` | `/api/v1/models/{model_id}` | One model, including deprecation status. |
| `POST` | `/api/v1/chat/completions` | Buffered completion. |
| `POST` | `/api/v1/chat/completions/stream` | SSE stream, OpenAI frame format. |
| `POST` | `/api/v1/chat/embeddings` | Text embeddings. |
| `POST` | `/api/v1/agent/run` | Autonomous copilot loop. SSE stream of plan, thought trace, tool calls, and final answer. |

### Autonomous copilot run

`POST /api/v1/agent/run` accepts `{"goal": str, "document_text": str, "max_steps": int}`
and streams `text/event-stream` frames (`plan_created`, `step_started`, `thought`,
`tool_result`, `step_completed`, `plan_updated`, `final_answer`, `run_failed`).
Each step routes to a Nemotron model chosen by `AIService`'s task routing —
Nano for summarisation/extraction, Ultra for auditing, Lightning for drafting.

```bash
curl -N http://localhost:8000/api/v1/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"goal": "Audit this vendor contract and draft an approval sign-off"}'
```

### Non-streaming completion

```bash
curl -s http://localhost:8000/api/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
        "model": "nvidia/Nemotron-3_5-Lightning",
        "messages": [{"role": "user", "content": "Explain mixture-of-experts in two sentences."}],
        "max_tokens": 256,
        "temperature": 0.6
      }'
```

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1790000000,
  "model": "nvidia/Nemotron-3_5-Lightning",
  "choices": [
    { "index": 0, "message": { "role": "assistant", "content": "..." }, "finish_reason": "stop" }
  ],
  "usage": { "prompt_tokens": 14, "completion_tokens": 52, "total_tokens": 66 },
  "provider": "nebius-token-factory",
  "latency_ms": 913
}
```

### Streaming completion

```bash
curl -N http://localhost:8000/api/v1/chat/completions/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Write a haiku about GPUs"}],"max_tokens":64}'
```

```
data: {"choices":[{"delta":{"content":"Silicon"}}],"index":0}

data: {"choices":[{"delta":{"content":" whispers"}}],"index":0}

data: [DONE]
```

Frames are proxied verbatim from Token Factory, so any OpenAI-compatible client
works against this gateway.

### Document ingestion

`POST /api/v1/documents/upload` accepts one multipart file in PDF, DOCX, TXT, or
CSV and returns its stored chunks. The pipeline is
`stage → validate → screen → extract → chunk → embed → persist`, and each stage
is independently testable.

```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H 'X-User-Email: alice@example.com' \
  -F 'file=@quarterly-report.pdf'
```

```json
{
  "id": "0f8c1d2e-...",
  "filename": "quarterly-report.pdf",
  "format": "pdf",
  "status": "ready",
  "page_count": 12,
  "chunk_count": 18,
  "token_count": 5410,
  "embedding_model": "Qwen/Qwen3-Embedding-8B",
  "embedded": true,
  "scan_engine": "clamav",
  "extraction": { "characters": 22140, "pages": 12, "metadata": { "pages_total": 12 } },
  "warnings": []
}
```

**Validation never trusts the client.** Both the `Content-Type` header and the
filename extension are attacker-controlled, so the format is decided by the
leading bytes. A `.txt` that is really a PDF, or a `.pdf` that is really a ZIP,
is rejected with 415 rather than mis-parsed. Filenames are reduced to a safe
basename and are never used as a path component — the stored name is a server-
generated UUID.

**Screening is layered, and honest about which layer ran.** An EICAR signature
check always runs locally. Set `CLAMAV_HOST` to add a ClamAV daemon over its
`INSTREAM` protocol. With no daemon configured the response reports
`scan_engine: eicar` and a warning saying so. A daemon that is unreachable
reports an *error*, never a clean result — a scanner that silently passes
everything is worse than no scanner.

**Extraction preserves what retrieval needs.** PDF page numbers and character
offsets survive into each chunk, so a hit can be traced back to page 4 of the
original. Formats with no real page concept — DOCX, TXT, CSV — report a single
page rather than inventing one per paragraph, because "page 3 of 4" on a
three-line document sends readers looking for something that does not exist.
CSV rows are rendered with their header keys, since a bare value is
unanswerable out of context, and DOCX tables are flattened row-per-line rather
than discarded.

**Chunking splits on meaning, not width.** Paragraphs are the primary unit;
oversized ones split on sentence boundaries, then words, and only then a hard
cut. Boundaries require whitespace, so `3.14` and `Dr. Chen` stay intact, and
an abbreviation guard keeps initials together. Every chunk satisfies
`text[char_start:char_end] == content`, so offsets always index the stored text
exactly.

**Embedding never blocks ingestion.** If the provider is down, slow, or returns
the wrong number of vectors, the chunks are still stored with their raw text and
the document is left in `embedding` status with the reason in `warnings`, ready
for a retry. Losing vectors is recoverable; losing the document is not.

**Nothing is left behind on failure.** Any stage that raises removes the staged
file, the promoted file, and the database row — a rejected upload leaves no
bytes on the storage volume.

Other endpoints: `GET /api/v1/documents` (yours only), 
`GET /api/v1/documents/{id}` (metadata + chunks), 
`GET /api/v1/documents/{id}/chunks`, 
`DELETE /api/v1/documents/{id}`. A document belonging to another user is
reported as **404, not 403** — revealing that an ID exists is itself a leak.

> Vectors are stored as JSON float lists rather than a native `pgvector` column
> so the same schema runs on SQLite in tests and Postgres in production.
> Cosine-similarity search over `document_chunks.embedding` is therefore an
> application-side scan today; moving to `pgvector` is a migration, not a
> redesign, when the corpus outgrows it.

### Errors

Every failure uses one shape, with a request ID that also appears in the logs:

```json
{
  "error": {
    "code": "NebiusAuthError",
    "message": "Nebius Token Factory rejected the API key (HTTP 401). Check NEBIUS_API_KEY.",
    "detail": { "error": "invalid api key" }
  },
  "request_id": "330a2ae5e6ef4b1c9fbd922fa584c8e0"
}
```

| Code | Status | Meaning |
|---|---|---|
| `ValidationError` | 422 | Request failed Pydantic validation. |
| `NebiusConfigurationError` | 503 | `NEBIUS_API_KEY` is not configured. |
| `NebiusAuthError` | 401 | Key rejected or out of quota. |
| `NebiusNotFoundError` | 404 | Unknown model, or blocked by `MODEL_ALLOWLIST`. |
| `NebiusRateLimitError` | 429 | Rate limited after retries were exhausted. |
| `NebiusUpstreamError` | 502 | Token Factory 5xx or malformed response. |

---

## Development

### Backend

```bash
cd backend && source .venv/bin/activate

pytest                    # no network or database required
pytest --cov              # with coverage
ruff check .              # lint
ruff format .             # format
ruff check . --fix        # autofix
mypy app tests            # strict type check
alembic revision --autogenerate -m "describe change"
alembic upgrade head
alembic downgrade -1
alembic upgrade head --sql    # print DDL without connecting
```

The test suite mocks Token Factory with `httpx.MockTransport` and exercises the
app in-process through ASGI, so it needs no database, no API key and no
network. It covers retry/backoff behaviour, auth-error mapping, SSE framing,
CORS, validation, model enrichment, deprecation handling and log redaction,
plus the persistence layer: ORM relationships and cascade rules, state-machine
transitions, ORM/migration drift, request and token metrics, and configuration
loading from the environment.

### Frontend

```bash
cd frontend

npm run dev            # dev server, hot reload
npm run build          # production build
npm run start          # serve the production build
npm run lint           # ESLint (flat config, eslint-config-next 16)
npm run typecheck      # tsc --noEmit, strict
npm run check          # all three, in order
```

`tsconfig.json` enables `strict`, `noUnusedLocals`, `noUnusedParameters` and
`noUncheckedIndexedAccess`.

### Code standards

- **Backend** — 100-column lines, `ruff` for lint and format, `mypy --strict`
  in spirit, Google-style docstrings enforced by `ruff`'s `D` rules, bandit
  (`S`) rules enabled.
- **Frontend** — `eslint-config-next` 16 (core-web-vitals + TypeScript +
  React Hooks), strict TypeScript, Tailwind v4 CSS-first `@theme` tokens.

---

## Security

- **Secrets stay out of git.** `.gitignore` excludes `.env`, `.env.*` and
  re-includes only `.env.example`.
- **Log redaction.** `app/core/logging.py` masks bearer tokens, API keys,
  passwords and JWTs in every log record, including interpolated arguments.
- **Non-root containers.** Both images run as UID 1001.
- **Bounded input.** Prompts are rejected above `LLM_MAX_INPUT_TOKENS`;
  `max_tokens` is clamped to `LLM_MAX_MAX_TOKENS`; `MODEL_ALLOWLIST` limits
  which model IDs can ever be invoked. Uploads are capped at `UPLOAD_MAX_BYTES`,
  which is enforced mid-stream rather than after buffering.
- **Uploads are validated by content.** The stored format comes from magic
  bytes, never the `Content-Type` header or the extension. Filenames are
  stripped to a safe basename and never form a path; stored names are
  server-generated UUIDs, which also prevents collisions.
- **Malware screening.** An EICAR signature check runs on every upload; set
  `CLAMAV_HOST` to add a real daemon. The response always names the engine that
  ran, so coverage is never overstated.
- **Failed uploads leave no residue.** Staged, promoted, and persisted state are
  all rolled back, so a rejected file does not linger in the storage volume.
- **Least-privilege DB.** The `nexusflow` role owns only its own database.
- **Graceful misconfiguration.** Production refuses to start on placeholder
  secrets or wildcard CORS.

Before deploying publicly, add authentication and per-user authorization — the
scaffold ships the JWT configuration and password hashing primitives, but no
login flow yet. Until then, `DEV_AUTH_ENABLED=true` (the default) lets any
client claim any identity through the `X-User-Email` header. Set it to `false`
in any environment reachable by untrusted clients, and replace
`app/api/deps.py:get_current_user` with real token verification.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `compose` exits: `NEBIUS_API_KEY is required` | Root `.env` not filled in | `cp .env.example .env`, add your key |
| `NebiusConfigurationError`, 503 | `NEBIUS_API_KEY` missing in `backend/.env` | Set it, restart the API |
| `NebiusAuthError`, 401 | Key invalid, revoked, or no credit | Create a new key; check billing |
| `NebiusNotFoundError`, 404 | Model ID retired | Use the `replacement` in the error; see deprecation notes above |
| `NebiusRateLimitError`, 429 | Quota or concurrency | Lower concurrency, raise quota, or lower `NEBIUS_MAX_RETRIES` |
| Browser CORS error | UI origin not allowed | Add the exact origin to `CORS_ORIGINS`, restart the API |
| UI says "Unreachable" | Backend not started or wrong URL | `docker compose ps`; check `NEXT_PUBLIC_API_BASE_URL` |
| UI shows stale config after env change | `NEXT_PUBLIC_*` is build-time | `docker compose build --no-cache frontend` |
| Backend restarts on boot | DB not ready | Normal on first run; raise `DB_WAIT_ATTEMPTS` if persistent |
| `pytest` collection errors | Wrong working directory | Run from `backend/` |
| Port already allocated | Something holds 3000/8000/5432 | Change `FRONTEND_PORT` / `BACKEND_PORT` / `POSTGRES_PORT` |

---

## Contributing

1. Branch from `main`.
2. Keep `ruff check .`, `mypy app tests`, `pytest` and `npm run check` green.
3. Add or update tests for behaviour changes.
4. Never commit secrets; document new env vars in the relevant `.env.example`.
5. Open a PR describing the change and its user-visible effect.

## Roadmap

- [ ] Authentication and per-user authorization (the JWT plumbing is in place)
- [ ] Workflow builder UI over the existing `Workflow` / `WorkflowRun` tables
- [ ] Token and cost analytics dashboard from the execution ledger
- [ ] Prompt versioning and A/B evaluation
- [ ] Vector store and retrieval-augmented workflows on `Qwen3-Embedding-8B`
- [ ] OpenTelemetry tracing across FastAPI → Token Factory
- [ ] Rate limiting and per-tenant quotas backed by Redis

---

## License

Copyright 2026 The NexusFlow AI Contributors.

Licensed under the [Apache License, Version 2.0](./LICENSE). You may obtain a
copy of the License at <http://www.apache.org/licenses/LICENSE-2.0>.

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.

Model weights are **not** distributed with this project. Refer to the
[NVIDIA Open Model License](https://build.nvidia.com/open-model-license) for
Nemotron, and to [Nebius](https://nebius.com/terms) for service terms.
