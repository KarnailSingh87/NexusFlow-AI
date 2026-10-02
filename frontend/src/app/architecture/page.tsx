const DIAGRAM = `
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
`

export const metadata = {
  title: 'Architecture · NexusFlow AI',
  description: 'How requests flow from the Next.js UI to NVIDIA Nemotron models.',
}

export default function ArchitecturePage() {
  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Architecture</h1>
        <p className="mt-1 text-sm text-ink-muted">
          The full request path, and the trust boundary it creates.
        </p>
      </header>

      <pre className="card overflow-x-auto text-xs leading-relaxed">{DIAGRAM}</pre>

      <section className="card space-y-3 text-sm">
        <h2 className="font-semibold">Why the backend is not optional</h2>
        <p className="text-ink-muted">
          The browser never receives <code className="font-mono">NEBIUS_API_KEY</code>.
          Only the FastAPI process holds it, which gives one place to enforce
          quotas, retry throttles, redact logs and record token spend.
        </p>
        <ul className="list-inside list-disc space-y-1 text-ink-muted">
          <li>
            <code className="font-mono">NEXT_PUBLIC_*</code> variables are embedded in
            the browser bundle — never prefix secrets with it.
          </li>
          <li>
            <code className="font-mono">INTERNAL_API_BASE_URL</code> is server-only and
            lets Route Handlers reach the backend over the Compose network.
          </li>
          <li>
            SSE frames are proxied verbatim, so the UI stays wire-compatible with the
            OpenAI streaming protocol.
          </li>
        </ul>
      </section>
    </div>
  )
}
