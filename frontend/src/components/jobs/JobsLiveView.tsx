'use client'

import { useEffect, useState } from 'react'

import { JobStateBadge } from '@/components/jobs/JobStateBadge'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'
import { apiBaseUrl, apiRoutes } from '@/lib/env'

interface JobStatus {
  id: string
  job_type: string
  state: string
  progress_pct: number
  error: string | null
  created_at: string
}

interface JobListResponse {
  items: JobStatus[]
  total: number
}

const POLL_MS = 3000

export function JobsLiveView() {
  const [jobs, setJobs] = useState<JobStatus[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}${apiRoutes.jobs}`, {
          headers: { Accept: 'application/json' },
        })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const body = (await response.json()) as JobListResponse
        if (active) {
          setJobs(body.items)
          setError(null)
        }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Failed to load jobs')
      }
    }
    void load()
    const timer = window.setInterval(load, POLL_MS)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])

  return (
    <Card>
      <CardHeader>
        <CardTitle>Background Jobs</CardTitle>
        <span className="text-xs text-ink-muted">Live · refreshes every 3s</span>
      </CardHeader>
      {error ? <p className="text-sm text-danger">Failed to load jobs: {error}</p> : null}
      {jobs.length === 0 && !error ? (
        <p className="text-sm text-ink-muted">No jobs yet.</p>
      ) : (
        <ul className="divide-y divide-edge/60">
          {jobs.map((job) => (
            <li key={job.id} className="flex items-center justify-between py-2.5 text-sm">
              <div>
                <p className="font-medium">{job.job_type}</p>
                <p className="font-mono text-[11px] text-ink-muted">{job.id}</p>
              </div>
              <div className="flex items-center gap-3">
                <div className="h-1.5 w-24 overflow-hidden rounded-full bg-surface-raised">
                  <div
                    className="h-full bg-brand transition-all"
                    style={{ width: `${job.progress_pct}%` }}
                  />
                </div>
                <JobStateBadge state={job.state} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
