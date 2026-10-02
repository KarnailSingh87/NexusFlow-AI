/**
 * Browser client for the autonomous copilot SSE stream.
 *
 * The backend streams `event: <kind>` + `data: {json}` frames from
 * `POST /api/v1/agent/run`; this parser buffers partial frames and yields one
 * typed event per complete frame.
 */

import { apiBaseUrl } from '@/lib/env'

export interface AgentEvent {
  type: string
  timestamp: number
  [key: string]: unknown
}

/** Parse raw SSE text into complete frames; returns leftover partial text. */
export function parseSseChunk(
  buffer: string,
): { events: AgentEvent[]; rest: string } {
  const events: AgentEvent[] = []
  const frames = buffer.split('\n\n')
  const rest = frames.pop() ?? ''
  for (const frame of frames) {
    let kind = 'message'
    let data = ''
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) kind = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).trim()
    }
    if (!data) continue
    try {
      const parsed = JSON.parse(data) as Record<string, unknown>
      events.push({ ...parsed, type: (parsed.type as string) ?? kind } as AgentEvent)
    } catch {
      // A malformed frame must not kill the stream.
    }
  }
  return { events, rest }
}

export async function* streamAgentRun(
  goal: string,
  documentText = '',
): AsyncGenerator<AgentEvent, void, undefined> {
  const response = await fetch(`${apiBaseUrl}/api/v1/agent/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ goal, document_text: documentText, max_steps: 8 }),
  })
  if (!response.ok || !response.body) {
    throw new Error(`Agent run failed (HTTP ${response.status})`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const { events, rest } = parseSseChunk(buffer)
      buffer = rest
      for (const event of events) yield event
    }
    const tail = parseSseChunk(`${buffer}\n\n`)
    for (const event of tail.events) yield event
  } finally {
    reader.releaseLock()
  }
}
