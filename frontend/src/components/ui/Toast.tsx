'use client'

import { AnimatePresence, motion } from 'framer-motion'
import { CheckCircle2, Info, TriangleAlert, XCircle } from 'lucide-react'
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { cn } from '@/lib/utils'

export type ToastTone = 'success' | 'error' | 'warning' | 'info'

interface Toast {
  id: number
  tone: ToastTone
  message: string
}

interface ToastContextValue {
  toast: (message: string, tone?: ToastTone) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

const TONE_STYLES: Record<ToastTone, string> = {
  success: 'border-ok/50 text-ok',
  error: 'border-danger/50 text-danger',
  warning: 'border-warn/50 text-warn',
  info: 'border-brand/50 text-brand',
}

const TONE_ICONS: Record<ToastTone, typeof Info> = {
  success: CheckCircle2,
  error: XCircle,
  warning: TriangleAlert,
  info: Info,
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const toast = useCallback((message: string, tone: ToastTone = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((current) => [...current, { id, tone, message }])
    window.setTimeout(() => {
      setToasts((current) => current.filter((t) => t.id !== id))
    }, 4000)
  }, [])

  const value = useMemo(() => ({ toast }), [toast])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
        <AnimatePresence>
          {toasts.map((t) => {
            const Icon = TONE_ICONS[t.tone]
            return (
              <motion.div
                key={t.id}
                initial={{ opacity: 0, x: 24 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 24 }}
                className={cn(
                  'flex items-start gap-2.5 rounded-lg border bg-surface-raised px-3.5 py-3 text-sm shadow-lg',
                  TONE_STYLES[t.tone],
                )}
              >
                <Icon className="mt-0.5 size-4 shrink-0" />
                <span className="text-ink">{t.message}</span>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within <ToastProvider>')
  return ctx
}
