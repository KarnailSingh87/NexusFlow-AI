'use client'

import { AnimatePresence, motion } from 'framer-motion'
import {
  Bot,
  BriefcaseBusiness,
  ChevronLeft,
  FileText,
  LayoutDashboard,
  Play,
  Network,
} from 'lucide-react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'

import { cn } from '@/lib/utils'

const NAV_ITEMS = [
  { href: '/', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/documents', label: 'Documents', icon: FileText },
  { href: '/copilot', label: 'Copilot', icon: Bot },
  { href: '/jobs', label: 'Jobs', icon: BriefcaseBusiness },
  { href: '/playground', label: 'Playground', icon: Play },
  { href: '/architecture', label: 'Architecture', icon: Network },
] as const

export interface SidebarProps {
  collapsed: boolean
  onToggle: () => void
}

export function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const pathname = usePathname()

  return (
    <motion.aside
      animate={{ width: collapsed ? 68 : 232 }}
      transition={{ duration: 0.2, ease: 'easeInOut' }}
      className="flex h-screen shrink-0 flex-col border-r border-edge/60 bg-surface/40"
    >
      <div className="flex h-14 items-center gap-2.5 border-b border-edge/60 px-4">
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-brand-strong font-bold text-white">
          N
        </span>
        <AnimatePresence>
          {!collapsed ? (
            <motion.span
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="truncate text-sm font-semibold"
            >
              NexusFlow AI
            </motion.span>
          ) : null}
        </AnimatePresence>
      </div>

      <nav className="flex flex-1 flex-col gap-1 p-3">
        {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
          const active = href === '/' ? pathname === '/' : pathname.startsWith(href)
          return (
            <Link
              key={href}
              href={href}
              title={label}
              className={cn(
                'flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm transition-colors',
                active
                  ? 'bg-brand/15 text-brand'
                  : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
              )}
            >
              <Icon className="size-4.5 shrink-0" />
              {!collapsed ? <span>{label}</span> : null}
            </Link>
          )
        })}
      </nav>

      <button
        type="button"
        onClick={onToggle}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        className="m-3 flex items-center justify-center rounded-lg border border-edge p-2 text-ink-muted transition-colors hover:text-ink"
      >
        <ChevronLeft
          className={cn('size-4 transition-transform', collapsed && 'rotate-180')}
        />
      </button>
    </motion.aside>
  )
}
