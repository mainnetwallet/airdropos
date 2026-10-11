/** Reusable presentational components. */

import type { ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, Info, Loader2, XCircle } from 'lucide-react'
import clsx from 'clsx'
import { TASK_STATUS_COLORS, type TaskStatus } from '@/types'

// ---------------------------------------------------------------- Card
export function Card({
  title,
  action,
  children,
  className,
}: {
  title?: string
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={clsx('card', className)}>
      {(title || action) && (
        <header className="mb-4 flex items-center justify-between">
          {title && <h2 className="card-title">{title}</h2>}
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

// ---------------------------------------------------------------- Stat
export function Stat({
  label,
  value,
  tone = 'neutral',
  hint,
}: {
  label: string
  value: ReactNode
  tone?: 'neutral' | 'success' | 'warning' | 'error' | 'info'
  hint?: string
}) {
  const toneClass = {
    neutral: 'text-slate-50',
    success: 'text-status-success',
    warning: 'text-status-warning',
    error: 'text-status-error',
    info: 'text-status-info',
  }[tone]

  return (
    <div className="card">
      <p className="stat-label">{label}</p>
      <p className={clsx('stat-value mt-2', toneClass)}>{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

// ---------------------------------------------------------------- Badge
export function StatusBadge({ status }: { status: TaskStatus | string }) {
  const cls = TASK_STATUS_COLORS[status as TaskStatus] ?? 'bg-slate-700 text-slate-300'
  return <span className={clsx('badge', cls)}>{status.replace(/_/g, ' ')}</span>
}

export function SeverityBadge({ severity }: { severity: string }) {
  const cls: Record<string, string> = {
    info: 'bg-blue-900 text-blue-300',
    success: 'bg-green-900 text-green-300',
    warning: 'bg-amber-900 text-amber-300',
    error: 'bg-red-900 text-red-300',
    critical: 'bg-red-700 text-white',
  }
  return (
    <span className={clsx('badge', cls[severity] ?? cls.info)}>{severity.toUpperCase()}</span>
  )
}

// ---------------------------------------------------------------- States
export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-12 text-slate-400">
      <Loader2 className="h-5 w-5 animate-spin" />
      <span>{label}…</span>
    </div>
  )
}

export function EmptyState({
  title,
  description,
  icon,
}: {
  title: string
  description?: string
  icon?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
      <div className="text-slate-600">{icon ?? <Info className="h-8 w-8" />}</div>
      <p className="font-medium text-slate-300">{title}</p>
      {description && <p className="max-w-md text-sm text-slate-500">{description}</p>}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  let message: string
  if (error instanceof Error) {
    message = error.message
  } else if (typeof error === 'string') {
    message = error
  } else {
    try {
      message = JSON.stringify(error)
    } catch {
      message = 'Unknown error'
    }
  }
  return (
    <div className="flex flex-col items-center gap-3 py-10 text-center">
      <XCircle className="h-8 w-8 text-status-error" />
      <p className="font-medium text-status-error">Something went wrong</p>
      <p className="max-w-lg text-sm text-slate-400">{message}</p>
      {onRetry && (
        <button onClick={onRetry} className="btn-ghost">
          Try again
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- Progress
export function ProgressBar({ value, className }: { value: number; className?: string }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100)
  const tone =
    pct >= 100 ? 'bg-status-success' : pct >= 50 ? 'bg-accent' : pct > 0 ? 'bg-status-info' : 'bg-slate-700'
  return (
    <div className={clsx('h-1.5 w-full overflow-hidden rounded-full bg-surface-300', className)}>
      <div className={clsx('h-full rounded-full transition-all', tone)} style={{ width: `${pct}%` }} />
    </div>
  )
}

// ---------------------------------------------------------------- Alert
export function Alert({
  kind = 'info',
  title,
  children,
}: {
  kind?: 'info' | 'warning' | 'error' | 'success'
  title?: string
  children: ReactNode
}) {
  const config = {
    info: { cls: 'border-status-info/40 bg-status-info/10 text-status-info', Icon: Info },
    warning: {
      cls: 'border-status-warning/40 bg-status-warning/10 text-status-warning',
      Icon: AlertTriangle,
    },
    error: { cls: 'border-status-error/40 bg-status-error/10 text-status-error', Icon: XCircle },
    success: {
      cls: 'border-status-success/40 bg-status-success/10 text-status-success',
      Icon: CheckCircle2,
    },
  }[kind]
  const Icon = config.Icon

  return (
    <div className={clsx('flex gap-3 rounded-lg border p-4', config.cls)}>
      <Icon className="h-5 w-5 shrink-0" />
      <div className="text-sm">
        {title && <p className="font-semibold">{title}</p>}
        <div className={title ? 'mt-1 opacity-90' : 'opacity-90'}>{children}</div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- Health dot
export function HealthDot({ status }: { status: string }) {
  const healthy = status === 'ok' || status === 'HEALTHY'
  const degraded = status === 'DEGRADED' || status === 'UNAVAILABLE' || status === 'STOPPED'
  return (
    <span className="inline-flex items-center gap-2 text-xs">
      <span
        className={clsx(
          'h-2 w-2 rounded-full',
          healthy ? 'bg-status-success' : degraded ? 'bg-status-warning' : 'bg-status-idle',
        )}
      />
      {status}
    </span>
  )
}
