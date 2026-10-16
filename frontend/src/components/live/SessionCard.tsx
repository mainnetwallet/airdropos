/** One card per real running session (browser, Telegram, future integrations). */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Clock, Maximize2, MousePointerClick, User } from 'lucide-react'
import clsx from 'clsx'
import LiveScreen from '@/components/live/LiveScreen'
import SessionActions from '@/components/live/SessionActions'
import ActivityList from '@/components/live/ActivityList'
import TelegramChat from '@/components/live/TelegramChat'
import { IntegrationBadge, StatusBadge } from '@/components/live/meta'
import { formatElapsed, useElapsed, useInView } from '@/hooks/useLive'
import { useChatView } from '@/hooks/useChatView'
import type { LiveEvent, LiveSession } from '@/types/live'
import type { StreamState } from '@/hooks/useLive'

/** Draws children at a fixed design size and scales the whole thing down to the card's width. */
function ScaledPreview({ width, height, interactive, children }: { width: number; height: number; interactive: boolean; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null)
  const [scale, setScale] = useState(0.5)
  useEffect(() => {
    const el = box.current
    if (!el) return
    const fit = () => setScale(Math.max(0.1, el.clientWidth / width))
    fit()
    const ro = new ResizeObserver(fit)
    ro.observe(el)
    return () => ro.disconnect()
  }, [width])
  return (
    <div ref={box} className="relative w-full overflow-hidden rounded-lg" style={{ height: height * scale }}>
      <div
        className={clsx('absolute left-0 top-0', !interactive && 'pointer-events-none select-none')}
        style={{ width, height, transform: `scale(${scale})`, transformOrigin: 'top left' }}
        aria-hidden={!interactive}
      >
        {children}
      </div>
    </div>
  )
}

export default function SessionCard({
  session,
  fetchedAt,
  canControl,
  events,
  onOpen,
  dense = false,
}: {
  session: LiveSession
  fetchedAt: number
  canControl: boolean
  events: LiveEvent[]
  onOpen: (session: LiveSession, enableClick: boolean) => void
  dense?: boolean
}) {
  const [ref, inView] = useInView<HTMLElement>()
  const [stream, setStream] = useState<StreamState>('loading')
  const elapsed = useElapsed(session, fetchedAt)
  const chat = useChatView(session.integration, session.key)
  const caps = session.capabilities
  const stopped = session.status === 'stopped'
  const onState = useCallback((s: StreamState) => setStream(s), [])
  const streaming = caps.visual && !stopped && stream === 'live'
  const own = events.filter((e) => e.session_id === session.id)

  return (
    <article
      ref={ref}
      className={clsx(
        'card flex flex-col gap-2.5 !p-3',
        session.status === 'waiting_for_user' && 'ring-2 ring-amber-500/70',
        session.status === 'failed' && 'ring-1 ring-red-500/50',
      )}
    >
      <header className="flex items-start gap-2">
        <IntegrationBadge id={session.integration} />
        <button type="button" onClick={() => onOpen(session, false)} className="min-w-0 flex-1 text-left">
          <p className="truncate text-sm font-bold text-slate-100">{session.name}</p>
          <p className="truncate text-xs text-slate-500">
            {session.subtitle || (session.integration === 'telegram' ? 'Telegram account activity' : 'No task attached')}
          </p>
        </button>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <StatusBadge status={session.status} streaming={streaming} />
          <span className="flex items-center gap-1 font-mono text-[11px] text-slate-500">
            <Clock className="h-3 w-3" /> {formatElapsed(elapsed)}
          </span>
        </div>
      </header>

      {/* Preview */}
      {caps.visual ? (
        <div className="relative">
          {stopped ? (
            <div className="flex aspect-[16/10] w-full items-center justify-center rounded-lg bg-slate-900/80 text-xs text-slate-400">
              Session stopped
            </div>
          ) : (
            <LiveScreen
              integration={session.integration}
              sessionKey={session.key}
              quality="thumb"
              enabled={inView}
              fit="width"
              fallbackSize={session.viewport ?? undefined}
              onState={onState}
            />
          )}
          {!stopped && (
            <button
              type="button"
              onClick={() => onOpen(session, false)}
              aria-label={`Open ${session.name} full screen`}
              className="absolute bottom-2 right-2 flex h-10 w-10 items-center justify-center rounded-lg bg-slate-900/70 text-white backdrop-blur hover:bg-slate-900/90"
            >
              <Maximize2 className="h-4 w-4" />
            </button>
          )}
        </div>
      ) : caps.chat ? (
        // A message-based account has no screen, so its preview is the whole chat UI drawn small. It is
        // view-only until Enable Click is pressed (then it works right here). Only the Maximize button opens the full view.
        <div className="relative">
          {stopped ? (
            <div className="flex aspect-[16/10] w-full items-center justify-center rounded-lg bg-slate-900/80 text-xs text-slate-400">
              Session stopped
            </div>
          ) : (
            <ScaledPreview width={720} height={450} interactive={chat.click && canControl}>
              <TelegramChat
                integration={session.integration}
                sessionKey={session.key}
                stopped={stopped}
                canWrite={canControl}
                compact
                active={inView}
                onOpenSession={(sess) => onOpen(sess, false)}
              />
            </ScaledPreview>
          )}
          {!stopped && (
            <button
              type="button"
              onClick={() => onOpen(session, false)}
              aria-label={`Open ${session.name} full screen`}
              className="absolute bottom-2 right-2 flex h-10 w-10 items-center justify-center rounded-lg bg-slate-900/70 text-white backdrop-blur hover:bg-slate-900/90"
            >
              <Maximize2 className="h-4 w-4" />
            </button>
          )}
        </div>
      ) : (
        <div className={clsx('rounded-lg bg-surface-200 p-2.5', dense ? 'min-h-[88px]' : 'min-h-[120px]')}>
          <ActivityList events={own} limit={3} emptyText="No activity yet" />
          <p className="mt-1 text-[11px] text-slate-500">{caps.chat ? (stopped ? 'Stopped and disconnected. Open it and press Reconnect.' : 'API session. Open it to see the live chats and reply.') : 'API session. It has no graphical screen to show or click.'}</p>
        </div>
      )}

      {/* Verification / error call-outs */}
      {session.status === 'waiting_for_user' && (
        <p className="rounded-lg bg-amber-500/10 px-2.5 py-1.5 text-xs font-medium text-amber-700">
          {session.waiting?.reason || 'Needs your action.'}
          {session.waiting?.timed_out && ' (no response for a long time)'}
        </p>
      )}
      {session.status === 'failed' && session.status_detail && (
        <p className="rounded-lg bg-red-500/10 px-2.5 py-1.5 text-xs text-red-600">{session.status_detail}</p>
      )}

      <p className="flex items-center gap-1.5 truncate text-xs text-slate-400">
        <User className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate">
          {session.profile ? `Profile: ${session.profile.name}` : session.account ? `Account: ${session.account}` : 'No profile'}
        </span>
        {session.control && <span className="ml-auto shrink-0 rounded bg-violet-500/15 px-1.5 text-[10px] font-semibold text-violet-600">YOU CONTROL</span>}
      </p>

      {/* Controls: only what this integration really supports */}
      <div className="flex flex-wrap items-center gap-2">
        {caps.manual_control && !stopped && (
          <button
            type="button"
            disabled={!canControl}
            onClick={() => onOpen(session, true)}
            title="Opens the full-screen view with Screen Click Mode on, so clicks land accurately"
            className="inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-lg bg-accent/10 px-3 text-xs font-semibold text-accent hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <MousePointerClick className="h-3.5 w-3.5" /> {dense ? 'Click' : 'Enable Click'}
          </button>
        )}
        {caps.chat && !stopped && (
          <button
            type="button"
            disabled={!canControl}
            onClick={chat.toggleClick}
            title="Switch clicking on or off right here in the card. The open chat is shared with the full screen."
            className={clsx(
              'inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-lg px-3 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50',
              chat.click ? 'bg-accent text-white hover:bg-accent-hover' : 'bg-accent/10 text-accent hover:bg-accent/20',
            )}
          >
            <MousePointerClick className="h-3.5 w-3.5" /> {chat.click ? (dense ? 'Click ON' : 'Click ON · Disable') : dense ? 'Click' : 'Enable Click'}
          </button>
        )}
        <SessionActions session={session} canControl={canControl} compact />
      </div>
    </article>
  )
}
