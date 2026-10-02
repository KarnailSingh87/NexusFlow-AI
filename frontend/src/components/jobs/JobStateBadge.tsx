import { Badge, type BadgeTone } from '@/components/ui/Badge'

const STATE_TONES: Record<string, BadgeTone> = {
  completed: 'success',
  processing: 'brand',
  queued: 'neutral',
  failed: 'danger',
  cancelled: 'warning',
}

export function JobStateBadge({ state }: { state: string }) {
  return <Badge tone={STATE_TONES[state] ?? 'neutral'}>{state}</Badge>
}
