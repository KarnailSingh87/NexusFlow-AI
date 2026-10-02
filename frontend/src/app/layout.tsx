import type { Metadata, Viewport } from 'next'
import Link from 'next/link'

import { appEnv, appName } from '@/lib/env'

import './globals.css'

export const metadata: Metadata = {
  title: `${appName} — Nemotron agents on Nebius`,
  description:
    'NexusFlow AI is an orchestration platform for NVIDIA Nemotron models served ' +
    'serverlessly by Nebius Token Factory.',
  icons: { icon: '/favicon.svg' },
}

export const viewport: Viewport = {
  themeColor: '#1b1b21',
}

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-full antialiased">
        <header className="border-b border-edge/60 bg-surface/40 backdrop-blur">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
            <Link href="/" className="flex items-center gap-2.5 no-underline">
              <span
                aria-hidden
                className="grid size-8 place-items-center rounded-lg bg-brand-strong font-bold text-white"
              >
                N
              </span>
              <span className="text-lg font-semibold tracking-tight">{appName}</span>
            </Link>
            <nav className="flex items-center gap-6 text-sm text-ink-muted">
              <Link href="/playground" className="transition-colors hover:text-ink">
                Playground
              </Link>
              <Link href="/architecture" className="transition-colors hover:text-ink">
                Architecture
              </Link>
              <span className="rounded-full border border-edge px-2.5 py-0.5 font-mono text-xs uppercase">
                {appEnv}
              </span>
            </nav>
          </div>
        </header>

        <main className="mx-auto max-w-6xl px-6 py-10">{children}</main>

        <footer className="mt-16 border-t border-edge/60 px-6 py-6 text-center text-xs text-ink-muted">
          Licensed under the Apache License 2.0 · Powered by NVIDIA Nemotron models via
          Nebius Token Factory
        </footer>
      </body>
    </html>
  )
}
