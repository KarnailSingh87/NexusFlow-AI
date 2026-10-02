'use client'

import { useEffect, useState } from 'react'

import { DocumentStatusBadge } from '@/components/documents/DocumentStatusBadge'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'
import { apiBaseUrl, apiRoutes } from '@/lib/env'

interface DocumentSummary {
  id: string
  filename: string
  status: string
  size_bytes: number | null
  chunk_count: number
  token_count: number
}

interface DocumentListResponse {
  items: DocumentSummary[]
  total: number
}

const POLL_MS = 5000

export function DocumentsLiveView() {
  const [docs, setDocs] = useState<DocumentSummary[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}${apiRoutes.documents}`, {
          headers: { Accept: 'application/json' },
        })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const body = (await response.json()) as DocumentListResponse
        if (active) {
          setDocs(body.items)
          setError(null)
        }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Failed to load documents')
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
        <CardTitle>Documents</CardTitle>
        <span className="text-xs text-ink-muted">Live · refreshes every 5s</span>
      </CardHeader>
      {error ? <p className="text-sm text-danger">Failed to load documents: {error}</p> : null}
      {docs.length === 0 && !error ? (
        <p className="text-sm text-ink-muted">No documents ingested yet.</p>
      ) : (
        <ul className="divide-y divide-edge/60">
          {docs.map((doc) => (
            <li key={doc.id} className="flex items-center justify-between py-2.5 text-sm">
              <div>
                <p className="font-medium">{doc.filename}</p>
                <p className="text-xs text-ink-muted">
                  {doc.chunk_count} chunks · {doc.token_count} tokens
                </p>
              </div>
              <DocumentStatusBadge status={doc.status} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
