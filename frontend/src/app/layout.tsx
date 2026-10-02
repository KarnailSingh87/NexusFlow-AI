import type { Metadata, Viewport } from 'next'

import { AppShell } from '@/components/layout/AppShell'
import { appName } from '@/lib/env'

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
    <html lang="en" className="dark">
      <body className="min-h-full antialiased">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  )
}
