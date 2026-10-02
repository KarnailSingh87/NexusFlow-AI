import { DocumentStatusBadge } from '@/components/documents/DocumentStatusBadge'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'

export default function DocumentsPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Documents</CardTitle>
      </CardHeader>
      <div className="flex gap-2">
        {['ready', 'processing', 'embedding', 'failed'].map((s) => (
          <DocumentStatusBadge key={s} status={s} />
        ))}
      </div>
      <p className="mt-4 text-sm text-ink-muted">Document ingestion UI lands here.</p>
    </Card>
  )
}
