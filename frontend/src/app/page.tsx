import Link from 'next/link'

import { getBackendStatus } from '@/lib/server-api'

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      aria-hidden
      className={`inline-block size-2.5 rounded-full ${ok ? 'bg-ok' : 'bg-danger'}`}
    />
  )
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat('en-US', { notation: 'compact' }).format(value)
}

export default async function HomePage() {
  const status = await getBackendStatus()
  const models = status.models?.data ?? []

  return (
    <div className="flex flex-col gap-10">
      <section className="max-w-3xl">
        <h1 className="text-4xl font-semibold tracking-tight">
          Orchestrate NVIDIA Nemotron agents, not GPUs.
        </h1>
        <p className="mt-4 text-lg leading-relaxed text-ink-muted">
          NexusFlow AI is a control plane for building, evaluating and running
          agent workflows on NVIDIA Nemotron checkpoints served serverlessly by
          Nebius Token Factory. One OpenAI-compatible API, dozens of open
          models, per-token billing.
        </p>
        <div className="mt-6 flex flex-wrap gap-3">
          <Link href="/playground" className="btn btn-primary no-underline">
            Open the playground
          </Link>
          <Link href="/architecture" className="btn no-underline">
            How it fits together
          </Link>
        </div>
      </section>

      <section className="grid gap-4 sm:grid-cols-3">
        <div className="card">
          <h2 className="text-sm font-semibold text-ink-muted">Control plane</h2>
          <p className="mt-2 font-mono text-xs">{process.env.INTERNAL_API_BASE_URL ?? 'http://localhost:8000'}</p>
          <p className="mt-3 flex items-center gap-2 text-sm">
            <StatusDot ok={status.reachable} />
            {status.reachable ? 'Reachable' : 'Unreachable'}
          </p>
          {status.error ? (
            <p className="mt-2 text-xs text-ink-muted">{status.error}</p>
          ) : null}
        </div>

        <div className="card">
          <h2 className="text-sm font-semibold text-ink-muted">API version</h2>
          <p className="mt-2 font-mono text-xs">{status.health?.version ?? '—'}</p>
          <p className="mt-3 text-sm capitalize">{status.health?.environment ?? 'unknown'}</p>
        </div>

        <div className="card">
          <h2 className="text-sm font-semibold text-ink-muted">Models in catalogue</h2>
          <p className="mt-2 text-2xl font-semibold">{models.length || '—'}</p>
          <p className="mt-3 text-sm text-ink-muted">
            source: {status.models?.source ?? 'unavailable'}
          </p>
        </div>
      </section>

      <section>
        <h2 className="text-xl font-semibold tracking-tight">Nemotron models</h2>
        <p className="mt-1 text-sm text-ink-muted">
          Live inventory proxied from Nebius Token Factory, enriched with curated
          context-window and pricing metadata.
        </p>

        {models.length === 0 ? (
          <div className="card mt-4 text-sm text-ink-muted">
            No models available. Set <code className="font-mono">NEBIUS_API_KEY</code>{' '}
            in <code className="font-mono">backend/.env</code> and restart the API.
          </div>
        ) : (
          <ul className="mt-4 grid gap-3 md:grid-cols-2">
            {models.map((model) => (
              <li key={model.id} className="card">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 className="font-medium">{model.label ?? model.id}</h3>
                    <p className="mt-0.5 font-mono text-xs text-ink-muted">{model.id}</p>
                  </div>
                  {model.tier ? (
                    <span className="rounded-full border border-edge px-2 py-0.5 text-xs uppercase text-ink-muted">
                      {model.tier}
                    </span>
                  ) : null}
                </div>

                {model.description ? (
                  <p className="mt-3 text-sm text-ink-muted">{model.description}</p>
                ) : null}

                <dl className="mt-4 grid grid-cols-3 gap-2 text-xs text-ink-muted">
                  <div>
                    <dt className="uppercase tracking-wide">Context</dt>
                    <dd className="mt-0.5 font-mono">
                      {model.context_tokens ? formatNumber(model.context_tokens) : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="uppercase tracking-wide">In / 1M</dt>
                    <dd className="mt-0.5 font-mono">
                      {model.input_cost_per_mtok_usd !== null
                        ? `$${model.input_cost_per_mtok_usd.toFixed(2)}`
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="uppercase tracking-wide">Out / 1M</dt>
                    <dd className="mt-0.5 font-mono">
                      {model.output_cost_per_mtok_usd !== null
                        ? `$${model.output_cost_per_mtok_usd.toFixed(2)}`
                        : '—'}
                    </dd>
                  </div>
                </dl>

                {model.deprecated ? (
                  <p className="mt-3 text-xs text-warn">
                    Deprecated — migrate to{' '}
                    <code className="font-mono">{model.replacement}</code>
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
