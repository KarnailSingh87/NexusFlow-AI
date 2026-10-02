'use client'

import { ArrowUp, Sparkles } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { ChatMessage, type ChatMessageData } from '@/components/copilot/ChatMessage'
import type { TraceEntry } from '@/components/copilot/ThoughtTrace'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { streamAgentRun } from '@/lib/agent'
import { cn } from '@/lib/utils'

const SUGGESTIONS = [
  'Summarize Q3 Financial Report',
  'Run Compliance Check on Contract #402',
  'Extract Invoice Line Items',
] as const

function traceFromEvent(event: { type: string } & Record<string, unknown>): TraceEntry | null {
  switch (event.type) {
    case 'plan_created': {
      const steps = (event.steps as { name?: string }[] | undefined) ?? []
      return {
        kind: 'plan',
        title: `Plan created — ${steps.length} step(s)`,
        detail: steps.map((s) => s.name).filter(Boolean).join(' → ') || undefined,
      }
    }
    case 'step_started':
      return { kind: 'progress', title: `Step: ${String(event.name)}`, detail: String(event.objective ?? '') }
    case 'thought':
      return { kind: 'thought', title: String(event.message ?? '') }
    case 'tool_result':
      return {
        kind: 'tool',
        title: `${String(event.tool)} completed`,
        detail: `model: ${String(event.model ?? 'unknown')} (${String(event.tier ?? '?')} tier)`,
      }
    case 'routing_update':
      return {
        kind: 'routing',
        title: 'Nemotron routing decision',
        detail: `${String(event.task_type ?? 'task')} → ${String(event.model ?? 'model')}`,
      }
    case 'step_failed':
      return { kind: 'error', title: `Step failed: ${String(event.name ?? '')}`, detail: String(event.error ?? '') }
    case 'run_failed':
      return { kind: 'error', title: 'Run failed', detail: String(event.error ?? '') }
    default:
      return null
  }
}

export function CopilotChat() {
  const [messages, setMessages] = useState<ChatMessageData[]>([])
  const [input, setInput] = useState('')
  const [running, setRunning] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const send = async (goal: string) => {
    const trimmed = goal.trim()
    if (!trimmed || running) return
    setInput('')
    setRunning(true)
    setMessages((current) => [
      ...current,
      { role: 'user', content: trimmed, trace: [] },
      { role: 'assistant', content: '', trace: [], streaming: true },
    ])

    const patchAssistant = (patch: (m: ChatMessageData) => ChatMessageData) =>
      setMessages((current) => current.map((m, i) => (i === current.length - 1 ? patch(m) : m)))

    try {
      for await (const event of streamAgentRun(trimmed)) {
        const trace = traceFromEvent(event as { type: string } & Record<string, unknown>)
        patchAssistant((m) => ({
          ...m,
          trace: trace ? [...m.trace, trace] : m.trace,
          content:
            event.type === 'final_answer'
              ? String(event.answer ?? '')
              : event.type === 'run_failed'
                ? `Run failed: ${String(event.error ?? 'unknown error')}`
                : m.content,
        }))
      }
    } catch (error) {
      patchAssistant((m) => ({
        ...m,
        content: `Connection error: ${error instanceof Error ? error.message : 'unknown'}`,
      }))
    } finally {
      patchAssistant((m) => ({ ...m, streaming: false }))
      setRunning(false)
    }
  }

  return (
    <Card className="flex h-[calc(100vh-8rem)] flex-col p-0">
      <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto p-5">
        {messages.length === 0 ? (
          <div className="grid h-full place-items-center text-center text-sm text-ink-muted">
            <div>
              <Sparkles className="mx-auto mb-2 size-6 text-brand" />
              Describe a goal and the copilot will plan, run tools, and report back.
            </div>
          </div>
        ) : (
          messages.map((message, index) => <ChatMessage key={index} message={message} />)
        )}
      </div>

      <div className="border-t border-edge/60 p-4">
        <div className="mb-3 flex flex-wrap gap-2">
          {SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => void send(suggestion)}
              disabled={running}
              className="rounded-full border border-edge bg-surface-raised px-3 py-1 text-xs text-ink-muted transition-colors hover:border-brand hover:text-ink disabled:opacity-50"
            >
              {suggestion}
            </button>
          ))}
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void send(input)
          }}
          className="flex items-center gap-2"
        >
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Ask the copilot to plan and execute a workflow…"
            className="field"
            disabled={running}
          />
          <Button
            type="submit"
            variant="primary"
            disabled={!input.trim() || running}
            aria-label="Send"
            className={cn('size-10 shrink-0 p-0')}
          >
            <ArrowUp className="size-4" />
          </Button>
        </form>
      </div>
    </Card>
  )
}
