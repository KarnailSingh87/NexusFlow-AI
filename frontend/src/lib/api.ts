/**
 * Typed client for the NexusFlow FastAPI control plane.
 *
 * All browser requests go through the Next.js dev/prod server to
 * `NEXT_PUBLIC_API_BASE_URL`; the backend answers CORS preflights using the
 * origins declared in `CORS_ORIGINS`.
 */

import { apiBaseUrl, apiRoutes, requestTimeoutMs } from '@/lib/env'
import type {
  ApiErrorBody,
  ChatCompletionRequest,
  ChatCompletionResponse,
  HealthResponse,
  ModelListResponse,
  StreamDelta,
} from '@/lib/types'

/** Normalised error surfaced to React components. */
export class NexusApiError extends Error {
  readonly code: string
  readonly status: number
  readonly requestId: string | null
  readonly detail: unknown

  constructor(message: string, options: {
    code?: string
    status?: number
    requestId?: string | null
    detail?: unknown
  } = {}) {
    super(message)
    this.name = 'NexusApiError'
    this.code = options.code ?? 'UnknownError'
    this.status = options.status ?? 0
    this.requestId = options.requestId ?? null
    this.detail = options.detail
  }
}

function buildUrl(path: string): string {
  return `${apiBaseUrl}${path}`
}

async function toNexusError(response: Response): Promise<NexusApiError> {
  let body: ApiErrorBody | null = null
  try {
    body = (await response.json()) as ApiErrorBody
  } catch {
    // Non-JSON error body (proxy timeout, gateway page, ...).
  }

  return new NexusApiError(
    body?.error?.message ?? `Request failed with HTTP ${response.status}`,
    {
      code: body?.error?.code ?? 'HttpError',
      status: response.status,
      requestId: body?.request_id ?? response.headers.get('X-Request-ID'),
      detail: body?.error?.detail,
    },
  )
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), requestTimeoutMs)

  try {
    const response = await fetch(buildUrl(path), {
      ...init,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...init?.headers,
      },
    })

    if (!response.ok) {
      throw await toNexusError(response)
    }
    return (await response.json()) as T
  } catch (error) {
    if (error instanceof NexusApiError) {
      throw error
    }
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new NexusApiError(`Request timed out after ${requestTimeoutMs}ms`, {
        code: 'Timeout',
      })
    }
    throw new NexusApiError(
      `Cannot reach the NexusFlow API at ${apiBaseUrl}. Is the backend running?`,
      { code: 'NetworkError' },
    )
  } finally {
    clearTimeout(timeout)
  }
}

export async function fetchHealth(): Promise<HealthResponse> {
  return request<HealthResponse>(apiRoutes.health)
}

export async function fetchModels(): Promise<ModelListResponse> {
  return request<ModelListResponse>(apiRoutes.models)
}

export async function createCompletion(
  payload: ChatCompletionRequest,
  signal?: AbortSignal,
): Promise<ChatCompletionResponse> {
  return request<ChatCompletionResponse>(apiRoutes.completions, {
    method: 'POST',
    body: JSON.stringify({ ...payload, stream: false }),
    signal,
  })
}

/**
 * Consume the SSE completion stream, invoking `onDelta` for each token chunk.
 *
 * Frames follow the OpenAI wire format (`data: {...}` terminated by
 * `data: [DONE]`), which the FastAPI layer forwards verbatim from Nebius.
 */
export async function streamCompletion(
  payload: ChatCompletionRequest,
  handlers: {
    onDelta: (text: string) => void
    onUsage?: (usage: StreamDelta['usage']) => void
    onError?: (error: NexusApiError) => void
    signal?: AbortSignal
  },
): Promise<void> {
  const response = await fetch(buildUrl(apiRoutes.completionsStream), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ ...payload, stream: true }),
    signal: handlers.signal,
  })

  if (!response.ok) {
    throw await toNexusError(response)
  }
  if (!response.body) {
    throw new NexusApiError('Streaming is not supported by this browser.', {
      code: 'NoBody',
    })
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const handleFrame = (frame: string): boolean => {
    const trimmed = frame.trim()
    if (!trimmed || trimmed.startsWith(':')) {
      return false
    }
    if (!trimmed.startsWith('data:')) {
      return false
    }

    const data = trimmed.slice(5).trim()
    if (data === '[DONE]') {
      return true
    }

    let parsed: StreamDelta
    try {
      parsed = JSON.parse(data) as StreamDelta
    } catch {
      return false
    }

    if (parsed.error) {
      handlers.onError?.(
        new NexusApiError(parsed.error.message, { code: parsed.error.code }),
      )
      return false
    }

    const delta = parsed.choices?.[0]?.delta?.content
    if (typeof delta === 'string' && delta.length > 0) {
      handlers.onDelta(delta)
    }
    if (parsed.usage) {
      handlers.onUsage?.(parsed.usage)
    }
    return false
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) {
        break
      }
      buffer += decoder.decode(value, { stream: true })

      // SSE frames are separated by a blank line; keep the partial tail in
      // the buffer for the next chunk.
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''

      for (const frame of frames) {
        if (handleFrame(frame)) {
          return
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}
