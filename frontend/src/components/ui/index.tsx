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

// ---------------------------------------------------------------- Page header
/** Page title block. The title is large on phones (as drawn in the reference) and 2xl on desktop. */
export function PageHeader({
  title,
  subtitle,
  action,
}: {
  title: ReactNode
  subtitle?: ReactNode
  action?: ReactNode
}) {
  return (
    <header className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-[26px] font-bold leading-tight text-slate-100 lg:text-2xl">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
      </div>
      {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
    </header>
  )
}

// ---------------------------------------------------------------- Filter chips
export interface ChipItem<T extends string> {
  id: T
  label: string
  /** Real count only; leave undefined when the number is not known. */
  count?: number
  disabled?: boolean
}

/** Horizontally scrollable pill filters (All 12 / Running 3 / ...). */
export function Chips<T extends string>({
  items,
  value,
  onChange,
  className,
}: {
  items: ChipItem<T>[]
  value: T
  onChange: (id: T) => void
  className?: string
}) {
  return (
    <div
      role="tablist"
      className={clsx('no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4 lg:mx-0 lg:flex-wrap lg:px-0', className)}
    >
      {items.map((item) => {
        const active = item.id === value
        return (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={active}
            disabled={item.disabled}
            onClick={() => onChange(item.id)}
            className={clsx(
              'shrink-0 whitespace-nowrap rounded-xl px-3.5 py-2 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-60',
              active
                ? 'bg-accent text-white shadow-sm'
                : 'bg-accent/10 text-slate-300 hover:bg-accent/15 lg:bg-surface-200 lg:text-slate-400 lg:hover:bg-surface-300',
            )}
          >
            {item.label}
            {item.count !== undefined && ` ${item.count}`}
          </button>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------- Segmented tabs
/** Equal-width tab strip (Goals / Strategies / Progress, Overview / Workers / Logs). */
export function SegmentedTabs<T extends string>({
  items,
  value,
  onChange,
  className,
}: {
  items: Array<{ id: T; label: string }>
  value: T
  onChange: (id: T) => void
  className?: string
}) {
  return (
    <div
      role="tablist"
      className={clsx('grid gap-1.5 rounded-2xl bg-surface-200 p-1 lg:hidden', className)}
      style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))` }}
    >
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={item.id === value}
          onClick={() => onChange(item.id)}
          className={clsx(
            'min-h-[40px] rounded-xl px-2 text-sm font-semibold transition-colors',
            item.id === value ? 'bg-accent text-white shadow-sm' : 'text-slate-400 hover:text-slate-200',
          )}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- Icon tile
/** Rounded square holding an icon, used as the leading element of list rows. */
export function IconTile({
  children,
  tone = 'blue',
  className,
}: {
  children: ReactNode
  tone?: 'blue' | 'green' | 'purple' | 'orange' | 'red' | 'cyan' | 'slate'
  className?: string
}) {
  const tones = {
    blue: 'bg-blue-500/15 text-blue-500',
    green: 'bg-emerald-500/15 text-emerald-500',
    purple: 'bg-violet-500/15 text-violet-500',
    orange: 'bg-orange-500/15 text-orange-500',
    red: 'bg-red-500/15 text-red-500',
    cyan: 'bg-cyan-500/15 text-cyan-500',
    slate: 'bg-slate-500/15 text-slate-400',
  }
  return (
    <span
      className={clsx(
        'flex h-11 w-11 shrink-0 items-center justify-center rounded-xl [&>svg]:h-5 [&>svg]:w-5',
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

/** Status pill used in list rows; colors follow the reference (green, blue, amber, red, gray). */
export function Pill({
  tone,
  children,
}: {
  tone: 'green' | 'blue' | 'amber' | 'red' | 'gray'
  children: ReactNode
}) {
  const tones = {
    green: 'bg-emerald-500/15 text-emerald-600',
    blue: 'bg-blue-500/15 text-blue-600',
    amber: 'bg-amber-500/15 text-amber-600',
    red: 'bg-red-500/15 text-red-600',
    gray: 'bg-slate-500/15 text-slate-400',
  }
  return (
    <span className={clsx('inline-flex items-center rounded-md px-2 py-1 text-[11px] font-semibold', tones[tone])}>
      {children}
    </span>
  )
}
