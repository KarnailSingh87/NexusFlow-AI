/**
 * Environment access.
 *
 * Anything prefixed with `NEXT_PUBLIC_` is inlined into the browser bundle at
 * build time. Treat every `NEXT_PUBLIC_*` value as public: provider keys and
 * database URLs must never carry that prefix.
 */

/** Browser-visible base URL of the FastAPI control plane. */
export const apiBaseUrl = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/+$/, '')

/** Server-only base URL, resolved from inside the container network. */
export const serverApiBaseUrl = (
  process.env.INTERNAL_API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/+$/, '')

export const appName = process.env.NEXT_PUBLIC_APP_NAME ?? 'NexusFlow AI'

export const appEnv = process.env.NEXT_PUBLIC_APP_ENV ?? 'development'

/** Model pre-selected in the playground. */
export const defaultModel =
  process.env.NEXT_PUBLIC_DEFAULT_MODEL ?? 'nvidia/Nemotron-3_5-Lightning'

/** Set to `false` to hide the streaming toggle in the playground. */
export const streamingEnabled = process.env.NEXT_PUBLIC_ENABLE_STREAMING !== 'false'

/** How long a single upstream request may take, in milliseconds. */
export const requestTimeoutMs = Number(process.env.NEXT_PUBLIC_API_TIMEOUT_MS ?? 120_000)

export const isProduction = appEnv === 'production'

/** Conventional route prefixes exposed by the backend. */
export const apiRoutes = {
  health: '/health',
  readiness: '/health/ready',
  models: '/api/v1/models',
  modelCatalog: '/api/v1/models/catalog',
  completions: '/api/v1/chat/completions',
  completionsStream: '/api/v1/chat/completions/stream',
  embeddings: '/api/v1/chat/embeddings',
} as const
