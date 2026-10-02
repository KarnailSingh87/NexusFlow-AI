'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  NexusApiError,
  createCompletion,
  fetchModels,
  streamCompletion,
} from '@/lib/api'
import { defaultModel, streamingEnabled } from '@/lib/env'
import type { ModelInfo, TokenUsage } from '@/lib/types'

interface Turn {
  id: number
  role: 'user' | 'assistant'
  content: string
}

let turnCounter = 0
const nextId = () => ++turnCounter

export function Playground() {
  const [models, setModels] = useState<ModelInfo[]>([])
  const [model, setModel] = useState(defaultModel)
  const [prompt, setPrompt] = useState('')
  const [temperature, setTemperature] = useState(0.6)
  const [maxTokens, setMaxTokens] = useState(2048)
  const [useStream, setUseStream] = useState(streamingEnabled)
  const [turns, setTurns] = useState<Turn[]>([])
  const [usage, setUsage] = useState<TokenUsage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loadingModels, setLoadingModels] = useState(true)

  const abortRef = useRef<AbortController | null>(null)
  const transcriptEndRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let cancelled = false

    fetchModels()
      .then((response) => {
        if (cancelled) return
        const usable = response.data.filter((entry) => !entry.deprecated)
        setModels(usable)
        // Prefer the server's idea of the default, fall back to the env value.
        setModel((current) =>
          usable.some((entry) => entry.id === current) ? current : response.default_model,
        )
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(
            cause instanceof NexusApiError
              ? cause.message
              : 'Unable to load the model catalogue.',
          )
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingModels(false)
      })

    return () => {
      cancelled = true
      abortRef.current?.abort()
    }
  }, [])

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns])

  const appendDelta = useCallback((assistantId: number, delta: string) => {
    setTurns((current) =>
      current.map((turn) =>
        turn.id === assistantId ? { ...turn, content: turn.content + delta } : turn,
      ),
    )
  }, [])

  const submit = useCallback(async () => {
    const trimmed = prompt.trim()
    if (!trimmed || busy) return

    setError(null)
    setUsage(null)
    setBusy(true)

    const userTurn: Turn = { id: nextId(), role: 'user', content: trimmed }
    const assistantTurn: Turn = { id: nextId(), role: 'assistant', content: '' }
    setTurns((current) => [...current, userTurn, assistantTurn])
    setPrompt('')

    const controller = new AbortController()
    abortRef.current = controller

    const history = [...turns, userTurn]
      .filter((turn) => turn.content.trim().length > 0)
      .map((turn) => ({ role: turn.role, content: turn.content }))

    try {
      if (useStream) {
        await streamCompletion(
          { model, messages: history, temperature, max_tokens: maxTokens },
          {
            signal: controller.signal,
            onDelta: (delta) => appendDelta(assistantTurn.id, delta),
            onUsage: (next) => next && setUsage(next),
            onError: (streamError) => setError(streamError.message),
          },
        )
      } else {
        const response = await createCompletion(
          { model, messages: history, temperature, max_tokens: maxTokens },
          controller.signal,
        )
        const content = response.choices[0]?.message.content
        setTurns((current) =>
          current.map((turn) =>
            turn.id === assistantTurn.id
              ? { ...turn, content: typeof content === 'string' ? content : '' }
              : turn,
          ),
        )
        setUsage(response.usage)
      }
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') {
        setTurns((current) => current.filter((turn) => turn.id !== assistantTurn.id))
      } else {
        setError(
          cause instanceof NexusApiError ? cause.message : 'The request failed unexpectedly.',
        )
        setTurns((current) => current.filter((turn) => turn.id !== assistantTurn.id))
      }
    } finally {
      abortRef.current = null
      setBusy(false)
    }
  }, [busy, maxTokens, model, prompt, temperature, turns, useStream, appendDelta])

  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
      <section className="card flex min-h-[32rem] flex-col">
        <div className="flex-1 space-y-4 overflow-y-auto pr-1">
          {turns.length === 0 ? (
            <p className="text-sm text-ink-muted">
              Ask {model} anything. Responses stream straight from Nebius Token
              Factory through the FastAPI gateway.
            </p>
          ) : (
            turns.map((turn) => (
              <article key={turn.id} className="space-y-1">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
                  {turn.role}
                </h3>
                <p className="whitespace-pre-wrap text-sm leading-relaxed">
                  {turn.content || (
                    <span className="text-ink-muted">
                      {busy ? 'Generating…' : 'No content returned.'}
                    </span>
                  )}
                </p>
              </article>
            ))
          )}
          <div ref={transcriptEndRef} />
        </div>

        {usage ? (
          <p className="border-t border-edge/60 pt-3 font-mono text-xs text-ink-muted">
            {usage.total_tokens} tokens · {usage.prompt_tokens} prompt ·{' '}
            {usage.completion_tokens} completion
          </p>
        ) : null}
      </section>

      <aside className="flex flex-col gap-4">
        <div className="card space-y-3">
          <label className="block text-xs font-semibold uppercase tracking-wide text-ink-muted">
            Model
          </label>
          <select
            className="field"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            disabled={busy}
          >
            {loadingModels ? <option>Loading catalogue…</option> : null}
            {models.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label ?? entry.id}
              </option>
            ))}
          </select>
          <p className="font-mono text-xs break-all text-ink-muted">{model}</p>
        </div>

        <div className="card space-y-3">
          <label
            htmlFor="temperature"
            className="block text-xs font-semibold uppercase tracking-wide text-ink-muted"
          >
            Temperature · {temperature.toFixed(2)}
          </label>
          <input
            id="temperature"
            type="range"
            min={0}
            max={2}
            step={0.1}
            value={temperature}
            onChange={(event) => setTemperature(Number(event.target.value))}
            disabled={busy}
            className="w-full"
          />

          <label
            htmlFor="max-tokens"
            className="block text-xs font-semibold uppercase tracking-wide text-ink-muted"
          >
            Max tokens · {maxTokens}
          </label>
          <input
            id="max-tokens"
            type="number"
            min={1}
            max={32768}
            value={maxTokens}
            onChange={(event) => setMaxTokens(Number(event.target.value))}
            disabled={busy}
            className="field"
          />
        </div>

        {streamingEnabled ? (
          <label className="card flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={useStream}
              onChange={(event) => setUseStream(event.target.checked)}
              disabled={busy}
            />
            Stream tokens via SSE
          </label>
        ) : null}

        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => void submit()}
            disabled={busy || !prompt.trim()}
            className="btn btn-primary flex-1 justify-center"
          >
            {busy ? 'Running…' : 'Send'}
          </button>
          {busy ? (
            <button type="button" onClick={stop} className="btn">
              Stop
            </button>
          ) : null}
        </div>

        {error ? (
          <p className="card border-danger/50 text-sm text-danger" role="alert">
            {error}
          </p>
        ) : null}
      </aside>
    </div>
  )
}
