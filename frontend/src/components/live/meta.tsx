/** Integration icons and status styling shared by every Live Broadcast surface. */

import type { ReactNode } from 'react'
import { Globe, MessagesSquare, Send } from 'lucide-react'
import type { SessionStatus } from '@/types/live'

export function XGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden>
      <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
    </svg>
  )
}

export function IntegrationIcon({ id, className = 'h-5 w-5' }: { id: string; className?: string }): JSX.Element {
  switch (id) {
    case 'browser':
      return <Globe className={className} />
    case 'telegram':
      return <Send className={className} />
    case 'x':
      return <XGlyph className={className} />
    case 'discord':
      return <MessagesSquare className={className} />
    default:
      return <Globe className={className} />
  }
}

export const INTEGRATION_TONE: Record<string, string> = {
  browser: 'bg-blue-500/15 text-blue-500',
  telegram: 'bg-sky-500/15 text-sky-500',
  x: 'bg-slate-500/15 text-slate-200',
  discord: 'bg-indigo-500/15 text-indigo-500',
}

export function IntegrationBadge({ id, size = 'md' }: { id: string; size?: 'sm' | 'md' }): JSX.Element {
  const box = size === 'sm' ? 'h-6 w-6' : 'h-9 w-9'
  return (
    <span
      className={`flex shrink-0 items-center justify-center rounded-full ${box} ${INTEGRATION_TONE[id] ?? 'bg-slate-500/15 text-slate-400'}`}
    >
      <IntegrationIcon id={id} className={size === 'sm' ? 'h-3.5 w-3.5' : 'h-[18px] w-[18px]'} />
    </span>
  )
}

export const STATUS_LABEL: Record<SessionStatus, string> = {
  running: 'Running',
  waiting_for_user: 'Waiting for you',
  paused: 'Paused',
  failed: 'Failed',
  stopped: 'Stopped',
}

export const STATUS_DOT: Record<SessionStatus, string> = {
  running: 'bg-emerald-500',
  waiting_for_user: 'bg-amber-500',
  paused: 'bg-slate-400',
  failed: 'bg-red-500',
  stopped: 'bg-slate-500',
}

export const STATUS_TILE: Record<SessionStatus, string> = {
  running: 'bg-emerald-500/10 text-emerald-600',
  waiting_for_user: 'bg-amber-500/10 text-amber-600',
  paused: 'bg-slate-500/10 text-slate-500',
  failed: 'bg-red-500/10 text-red-600',
  stopped: 'bg-slate-500/10 text-slate-500',
}

/** The LIVE badge is only honest for a visual session whose frames are really arriving. */
export function StatusBadge({
  status,
  streaming,
}: {
  status: SessionStatus
  streaming: boolean
}): JSX.Element {
  let text: ReactNode = STATUS_LABEL[status]
  let tone = 'bg-slate-500/15 text-slate-500'
  if (status === 'running') {
    text = streaming ? 'LIVE' : 'Running'
    tone = streaming ? 'bg-red-500 text-white' : 'bg-emerald-500/15 text-emerald-600'
  } else if (status === 'waiting_for_user') {
    text = 'NEEDS YOU'
    tone = 'animate-pulse bg-amber-500 text-white'
  } else if (status === 'failed') {
    tone = 'bg-red-500/15 text-red-600'
  }
  return <span className={`shrink-0 rounded-md px-2 py-0.5 text-[11px] font-bold tracking-wide ${tone}`}>{text}</span>
}
