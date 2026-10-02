import { Badge, type BadgeTone } from '@/components/ui/Badge'

const STATUS_TONES: Record<string, BadgeTone> = {
  ready: 'success',
  processing: 'warning',
  embedding: 'warning',
  uploaded: 'brand',
  pending: 'neutral',
  failed: 'danger',
}

export function DocumentStatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONES[status] ?? 'neutral'}>{status}</Badge>
}
