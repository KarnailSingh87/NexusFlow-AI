'use client'

import { ChevronRight } from 'lucide-react'
import Link from 'next/link'
import { Fragment } from 'react'

import { Badge } from '@/components/ui/Badge'
import { cn } from '@/lib/utils'

const STATUS_INDICATORS = [
  { label: 'API', ok: true },
  { label: 'Inference', ok: true },
  { label: 'DB', ok: true },
] as const

export function Header({ pathname }: { pathname: string }) {
  const crumbs = pathname.split('/').filter(Boolean)

  return (
    <header className="sticky top-0 z-40 flex h-14 items-center justify-between border-b border-edge/60 bg-canvas/80 px-6 backdrop-blur">
      <nav aria-label="Breadcrumb" className="flex items-center gap-1.5 text-sm">
        <Link href="/" className="text-ink-muted transition-colors hover:text-ink">
          Home
        </Link>
        {crumbs.map((segment, index) => {
          const href = `/${crumbs.slice(0, index + 1).join('/')}`
          const last = index === crumbs.length - 1
          return (
            <Fragment key={href}>
              <ChevronRight className="size-3.5 text-ink-muted" />
              <Link
                href={href}
                className={cn(
                  'capitalize transition-colors',
                  last ? 'text-ink' : 'text-ink-muted hover:text-ink',
                )}
              >
                {segment.replace(/-/g, ' ')}
              </Link>
            </Fragment>
          )
        })}
      </nav>

      <div className="flex items-center gap-4">
        <div className="hidden items-center gap-3 sm:flex">
          {STATUS_INDICATORS.map(({ label, ok }) => (
            <span key={label} className="flex items-center gap-1.5 text-xs text-ink-muted">
              <span
                className={cn(
                  'size-1.5 rounded-full',
                  ok ? 'animate-pulse bg-ok' : 'bg-danger',
                )}
              />
              {label}
            </span>
          ))}
        </div>
        <Badge tone="brand">Nemotron</Badge>
        <div
          aria-label="User profile"
          className="grid size-8 place-items-center rounded-full bg-brand/20 text-xs font-semibold text-brand"
        >
          HA
        </div>
      </div>
    </header>
  )
}
