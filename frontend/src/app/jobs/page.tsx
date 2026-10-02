import { JobStateBadge } from '@/components/jobs/JobStateBadge'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'

export default function JobsPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Background Jobs</CardTitle>
      </CardHeader>
      <div className="flex gap-2">
        {['queued', 'processing', 'completed', 'failed'].map((s) => (
          <JobStateBadge key={s} state={s} />
        ))}
      </div>
      <p className="mt-4 text-sm text-ink-muted">Job queue and telemetry UI lands here.</p>
    </Card>
  )
}
