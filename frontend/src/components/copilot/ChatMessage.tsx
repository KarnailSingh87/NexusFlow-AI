'use client'

import { Bot, User } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { ThoughtTrace, type TraceEntry } from '@/components/copilot/ThoughtTrace'
import { cn } from '@/lib/utils'

export interface ChatMessageData {
  role: 'user' | 'assistant'
  content: string
  trace: TraceEntry[]
  streaming?: boolean
}

export function ChatMessage({ message }: { message: ChatMessageData }) {
  const assistant = message.role === 'assistant'

  return (
    <div className={cn('flex gap-3', !assistant && 'flex-row-reverse')}>
      <div
        aria-hidden
        className={cn(
          'grid size-8 shrink-0 place-items-center rounded-lg',
          assistant ? 'bg-brand/20 text-brand' : 'bg-surface-raised text-ink-muted',
        )}
      >
        {assistant ? <Bot className="size-4.5" /> : <User className="size-4.5" />}
      </div>
      <div
        className={cn(
          'min-w-0 max-w-[78%] rounded-xl border px-4 py-3 text-sm',
          assistant ? 'border-edge bg-surface' : 'border-brand/40 bg-brand/10',
        )}
      >
        {message.content ? (
          <div className="prose prose-invert prose-sm max-w-none [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        ) : message.streaming ? (
          <span className="inline-block size-2 animate-pulse rounded-full bg-brand" />
        ) : null}
        {assistant ? (
          <ThoughtTrace entries={message.trace} streaming={message.streaming} />
        ) : null}
      </div>
    </div>
  )
}
