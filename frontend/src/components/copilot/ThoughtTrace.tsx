'use client'

import { AnimatePresence, motion } from 'framer-motion'
import {
  Brain,
  CheckCircle2,
  ChevronDown,
  ListChecks,
  Route,
  Wrench,
  XCircle,
} from 'lucide-react'
import { useState } from 'react'

import { cn } from '@/lib/utils'

export interface TraceEntry {
  kind: 'plan' | 'thought' | 'tool' | 'routing' | 'progress' | 'error'
  title: string
  detail?: string
}

const ICONS = {
  plan: ListChecks,
  thought: Brain,
  tool: Wrench,
  routing: Route,
  progress: CheckCircle2,
  error: XCircle,
} as const

export function ThoughtTrace({ entries, streaming }: { entries: TraceEntry[]; streaming?: boolean }) {
  const [open, setOpen] = useState(false)

  if (entries.length === 0) return null

  return (
    <div className="mt-3 rounded-lg border border-edge bg-canvas/60">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-xs font-medium text-ink-muted transition-colors hover:text-ink"
        aria-expanded={open}
      >
        <span className="flex items-center gap-1.5">
          <Brain className="size-3.5" />
          Agent Thought Trace
          <span className="rounded-full bg-surface-raised px-1.5 py-px text-[10px]">
            {entries.length}
          </span>
          {streaming ? (
            <span className="size-1.5 animate-pulse rounded-full bg-brand" />
          ) : null}
        </span>
        <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} />
      </button>
      <AnimatePresence initial={false}>
        {open ? (
          <motion.ol
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden border-t border-edge/60 px-3 py-2"
          >
            {entries.map((entry, index) => {
              const Icon = ICONS[entry.kind]
              return (
                <li key={index} className="flex gap-2.5 py-1.5 text-xs">
                  <Icon
                    className={cn(
                      'mt-0.5 size-3.5 shrink-0',
                      entry.kind === 'error' ? 'text-danger' : 'text-brand',
                    )}
                  />
                  <div>
                    <p className="text-ink">{entry.title}</p>
                    {entry.detail ? (
                      <p className="mt-0.5 font-mono text-[11px] text-ink-muted">
                        {entry.detail}
                      </p>
                    ) : null}
                  </div>
                </li>
              )
            })}
          </motion.ol>
        ) : null}
      </AnimatePresence>
    </div>
  )
}
