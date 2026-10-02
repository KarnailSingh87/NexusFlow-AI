import type { HTMLAttributes } from 'react'

import { cn } from '@/lib/utils'

export type BadgeTone = 'neutral' | 'success' | 'warning' | 'danger' | 'brand'

const TONES: Record<BadgeTone, string> = {
  neutral: 'border-edge text-ink-muted',
  success: 'border-ok/50 text-ok',
  warning: 'border-warn/50 text-warn',
  danger: 'border-danger/50 text-danger',
  brand: 'border-brand/50 text-brand',
}

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone
}

export function Badge({ className, tone = 'neutral', ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium',
        TONES[tone],
        className,
      )}
      {...props}
    />
  )
}
