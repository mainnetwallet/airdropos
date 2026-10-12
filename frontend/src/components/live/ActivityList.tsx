/** Real activity events only. Empty means nothing has happened, never "demo" rows. */

import { AlertTriangle, Camera, CheckCircle2, Circle, Globe, Hand, MousePointerClick, Pause, Play, Power, Send, ShieldAlert } from 'lucide-react'
import clsx from 'clsx'
import type { LiveEvent, LiveSession } from '@/types/live'
import { IntegrationBadge } from '@/components/live/meta'

function iconFor(event: LiveEvent): JSX.Element {
  const cls = 'h-4 w-4'
  const t = event.type
  if (event.status === 'error') return <AlertTriangle className={clsx(cls, 'text-red-500')} />
  if (t === 'browser.navigated') return <Globe className={clsx(cls, 'text-blue-500')} />
  if (t === 'browser.clicked' || t === 'control.click') return <MousePointerClick className={clsx(cls, 'text-blue-500')} />
  if (t === 'browser.screenshot') return <Camera className={clsx(cls, 'text-sky-500')} />
  if (t.startsWith('verification.')) {
    return t === 'verification.cleared' || t === 'verification.verified'
      ? <CheckCircle2 className={clsx(cls, 'text-emerald-500')} />
      : <ShieldAlert className={clsx(cls, 'text-amber-500')} />
  }
  if (t.startsWith('control.')) return <Hand className={clsx(cls, 'text-violet-500')} />
  if (t === 'session.paused') return <Pause className={clsx(cls, 'text-slate-500')} />
  if (t === 'session.resumed') return <Play className={clsx(cls, 'text-emerald-500')} />
  if (t.startsWith('session.')) return <Power className={clsx(cls, 'text-slate-500')} />
  if (t.startsWith('telegram.')) return <Send className={clsx(cls, 'text-sky-500')} />
  return <Circle className={clsx(cls, 'text-slate-500')} />
}

export function formatClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString([], { hour12: false })
}

export default function ActivityList({
  events,
  sessions,
  showSession = false,
  limit,
  emptyText = 'No activity yet.',
  className,
}: {
  events: LiveEvent[]
  sessions?: LiveSession[]
  showSession?: boolean
  limit?: number
  emptyText?: string
  className?: string
}) {
  const rows = limit ? events.slice(0, limit) : events
  if (rows.length === 0) return <p className="py-6 text-center text-sm text-slate-500">{emptyText}</p>
  const names = new Map((sessions ?? []).map((s) => [s.id, s.name]))
  return (
    <ul className={clsx('divide-y divide-surface-300/60', className)}>
      {rows.map((e) => (
        <li key={e.id} className="flex items-start gap-2.5 py-2">
          <span className="w-[58px] shrink-0 pt-0.5 font-mono text-[11px] text-slate-500">{formatClock(e.at)}</span>
          {showSession ? <IntegrationBadge id={e.integration} size="sm" /> : <span className="pt-0.5">{iconFor(e)}</span>}
          <div className="min-w-0 flex-1">
            {showSession && (
              <p className="truncate text-xs font-semibold text-slate-200">
                {(e.session_id && names.get(e.session_id)) || e.session_id || 'System'}
              </p>
            )}
            <p className="break-words text-xs leading-snug text-slate-400">{e.message}</p>
          </div>
        </li>
      ))}
    </ul>
  )
}
