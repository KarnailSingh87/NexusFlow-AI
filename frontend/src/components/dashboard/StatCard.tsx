import type { LucideIcon } from 'lucide-react'

import { Card } from '@/components/ui/Card'
import { cn } from '@/lib/utils'

export interface StatCardProps {
  label: string
  value: string
  delta?: string
  icon: LucideIcon
  tone?: 'brand' | 'ok' | 'warn' | 'danger'
}

const TONES = {
  brand: 'text-brand',
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
} as const

export function StatCard({ label, value, delta, icon: Icon, tone = 'brand' }: StatCardProps) {
  return (
    <Card className="flex items-start justify-between">
      <div>
        <p className="text-xs uppercase tracking-wide text-ink-muted">{label}</p>
        <p className="mt-1.5 text-2xl font-semibold tracking-tight">{value}</p>
        {delta ? <p className="mt-1 text-xs text-ink-muted">{delta}</p> : null}
      </div>
      <Icon className={cn('size-5', TONES[tone])} />
    </Card>
  )
}
