import { Playground } from '@/components/playground/Playground'

export const metadata = {
  title: 'Playground · NexusFlow AI',
  description: 'Send prompts to NVIDIA Nemotron models through the NexusFlow gateway.',
}

export default function PlaygroundPage() {
  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Playground</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Requests travel browser → Next.js → FastAPI → Nebius Token Factory →
          NVIDIA Nemotron. The FastAPI key is never sent to the browser.
        </p>
      </header>
      <Playground />
    </div>
  )
}
