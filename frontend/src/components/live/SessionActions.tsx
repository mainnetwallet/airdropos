/** Pause / Resume / Stop / Resume Agent for a session, shared by cards and the expanded view. */

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Pause, Play, Square } from 'lucide-react'
import clsx from 'clsx'
import { liveApi as api } from '@/services/api'
import type { LiveSession } from '@/types/live'

export function useSessionActions(session: LiveSession) {
  const qc = useQueryClient()
  const [message, setMessage] = useState<string | null>(null)
  const done = () => qc.invalidateQueries({ queryKey: ['live'] })
  const fail = (e: unknown) => setMessage(e instanceof Error ? e.message : 'Action failed')

  const pause = useMutation({
    mutationFn: () => api.livePause(session.integration, session.key),
    onSuccess: () => { setMessage(null); void done() },
    onError: fail,
  })
  const resume = useMutation({
    mutationFn: () => api.liveResume(session.integration, session.key),
    onSuccess: (r) => {
      setMessage(r.task_requeued ? `Agent resumed. Task ${r.task_requeued} was re-queued.` : 'Agent resumed.')
      void done()
    },
    onError: fail,
  })
  const stop = useMutation({
    mutationFn: () => api.liveStop(session.integration, session.key),
    onSuccess: () => { setMessage(null); void done() },
    onError: fail,
  })

  const confirmStop = () => {
    const ok = window.confirm(
      `Stop ${session.name}? ${
        session.integration === 'browser'
          ? 'The browser is closed (saved cookies are kept). A linked task stays parked until you cancel or re-run it.'
          : 'The Telegram client is disconnected until you resume it.'
      }`,
    )
    if (ok) stop.mutate()
  }

  return { pause, resume, stop, confirmStop, message, clearMessage: () => setMessage(null) }
}

const BASE =
  'inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-lg px-3 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50'

export default function SessionActions({
  session,
  canControl,
  compact = false,
  className,
}: {
  session: LiveSession
  canControl: boolean
  compact?: boolean
  className?: string
}) {
  const a = useSessionActions(session)
  const caps = session.capabilities
  if (session.status === 'stopped') return null
  const busy = a.pause.isPending || a.resume.isPending || a.stop.isPending
  const isParked = session.status === 'paused' || session.status === 'waiting_for_user'

  return (
    <div className={clsx('flex flex-wrap items-center gap-2', className)}>
      {caps.pause_resume &&
        (isParked ? (
          <button
            type="button"
            disabled={!canControl || busy}
            onClick={() => a.resume.mutate()}
            className={clsx(BASE, session.status === 'waiting_for_user' ? 'bg-emerald-600 text-white hover:bg-emerald-700' : 'bg-surface-200 text-slate-200 hover:bg-surface-300')}
          >
            {a.resume.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            {session.status === 'waiting_for_user' ? 'Resume Agent' : 'Resume'}
          </button>
        ) : (
          <button type="button" disabled={!canControl || busy || session.status === 'failed'} onClick={() => a.pause.mutate()} className={clsx(BASE, 'bg-surface-200 text-slate-200 hover:bg-surface-300')}>
            <Pause className="h-3.5 w-3.5" /> Pause
          </button>
        ))}
      {caps.stop && (
        <button type="button" disabled={!canControl || busy} onClick={a.confirmStop} className={clsx(BASE, 'bg-red-500/10 text-red-600 hover:bg-red-500/20')}>
          <Square className="h-3.5 w-3.5 fill-current" /> Stop
        </button>
      )}
      {a.message && <p className="w-full text-xs text-amber-600">{a.message}</p>}
    </div>
  )
}
