'use client'

import { useState } from 'react'

import { Button } from '@/components/ui/Button'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'

export function CopilotPanel() {
  const [goal, setGoal] = useState('')

  return (
    <Card>
      <CardHeader>
        <CardTitle>Agentic Copilot</CardTitle>
        <span className="text-xs text-ink-muted">Planner · Tools · Trace</span>
      </CardHeader>
      <textarea
        value={goal}
        onChange={(event) => setGoal(event.target.value)}
        placeholder="Describe a high-level goal, e.g. audit this vendor contract…"
        className="field mb-3 min-h-24 resize-y"
      />
      <Button variant="primary" disabled={!goal.trim()}>
        Run agent
      </Button>
    </Card>
  )
}
