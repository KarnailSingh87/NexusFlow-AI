/** Wire types mirroring the FastAPI schemas in `backend/app/schemas`. */

export interface TokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool' | 'developer'
  content: string | ContentPart[]
  name?: string
  tool_call_id?: string
}

export interface ContentPart {
  type: string
  text?: string
  image_url?: { url: string; detail?: string }
}

export interface ChatCompletionRequest {
  model?: string
  messages: Array<Pick<ChatMessage, 'role' | 'content'>>
  temperature?: number
  max_tokens?: number
  top_p?: number
  stop?: string[]
  stream?: boolean
}

export interface ChatCompletionResponse {
  id: string
  object: string
  created: number
  model: string
  choices: Array<{
    index: number
    message: { role: string; content: string | ContentPart[] }
    finish_reason: string | null
  }>
  usage: TokenUsage
  provider: string
  latency_ms: number | null
}

export interface ModelInfo {
  id: string
  label: string | null
  tier: string | null
  family: string | null
  description: string | null
  context_tokens: number | null
  max_output_tokens: number | null
  input_cost_per_mtok_usd: number | null
  output_cost_per_mtok_usd: number | null
  supports_tools: boolean | null
  supports_vision: boolean | null
  owned_by: string | null
  created: number | null
  deprecated: boolean
  replacement: string | null
  curated: boolean
}

export interface ModelListResponse {
  object: string
  default_model: string
  source: 'live' | 'curated'
  data: ModelInfo[]
}

export interface HealthResponse {
  status: 'ok' | 'degraded' | 'unavailable'
  version: string
  environment: string
  checks: Record<string, unknown>
}

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    detail?: unknown
  }
  request_id?: string | null
}

/** Incremental payload delivered by the SSE stream. */
export interface StreamDelta {
  choices?: Array<{ delta?: { content?: string | null }; finish_reason?: string | null }>
  usage?: TokenUsage
  error?: { code: string; message: string }
}
