/**
 * Expanded session view: the live screen gets (almost) the whole viewport.
 *
 * Desktop: slim top bar, big stage, collapsible activity drawer.
 * Phone: the detail screen from the reference (preview, controls, profile/URL rows, tabs) with
 * an "Open in Full Screen" mode that hands the entire display to the live screen.
 *
 * Screen Click Mode is OFF until the operator turns it on, and turning it on takes the
 * exclusive server-side lock (see useSessionControl).
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft, ChevronRight, ClipboardList, Hand, Keyboard, Link2, Loader2, Maximize2, Minimize2,
  MousePointerClick, MousePointer2, PanelRightClose, PanelRightOpen, ShieldAlert, WifiOff,
} from 'lucide-react'
import clsx from 'clsx'
import LiveScreen from '@/components/live/LiveScreen'
import ActivityList from '@/components/live/ActivityList'
import SessionActions from '@/components/live/SessionActions'
import { IntegrationBadge, StatusBadge, STATUS_DOT, STATUS_LABEL } from '@/components/live/meta'
import { formatElapsed, useElapsed, useLiveFeed, useMediaQuery, useSessionControl, useStale, type StreamState } from '@/hooks/useLive'
import type { LiveSession } from '@/types/live'

type ControlApi = ReturnType<typeof useSessionControl>

// ------------------------------------------------------------------ control bar
function ControlBar({
  session,
  control,
  canControl,
  immersive,
  onToggleFullscreen,
  dark = false,
}: {
  session: LiveSession
  control: ControlApi
  canControl: boolean
  immersive: boolean
  onToggleFullscreen: () => void
  dark?: boolean
}) {
  const caps = session.capabilities
  const stopped = session.status === 'stopped'
  const lockedByOther = !!session.control && !control.active
  const [text, setText] = useState('')
  const base = 'inline-flex min-h-[40px] items-center justify-center gap-1.5 rounded-lg px-3 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50'
  const idle = dark ? 'bg-white/15 text-white hover:bg-white/25' : 'bg-surface-200 text-slate-200 hover:bg-surface-300'

  const sendText = () => {
    if (!text) return
    control.send({ type: 'text', text })
    setText('')
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {caps.manual_control && !stopped && (
          <>
            <button
              type="button"
              disabled={!canControl || control.busy || lockedByOther}
              onClick={() => (control.active ? control.disable() : control.enable('click'))}
              className={clsx(base, control.active ? 'bg-accent text-white hover:bg-accent-hover' : 'bg-accent/15 text-accent hover:bg-accent/25')}
            >
              {control.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <MousePointerClick className="h-4 w-4" />}
              {control.active ? 'Disable Click' : 'Enable Click'}
            </button>
            {caps.keyboard && (
              <button
                type="button"
                disabled={!canControl || control.busy || lockedByOther}
                onClick={() => (control.mode === 'full' ? control.disable() : control.enable('full'))}
                className={clsx(base, control.mode === 'full' ? 'bg-violet-600 text-white hover:bg-violet-700' : idle)}
              >
                <Hand className="h-4 w-4" />
                {control.mode === 'full' ? 'Release Control' : 'Take Control'}
              </button>
            )}
          </>
        )}
        <SessionActions session={session} canControl={canControl} compact />
        <button type="button" onClick={onToggleFullscreen} className={clsx(base, idle, 'ml-auto')}>
          {immersive ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
          <span className="hidden sm:inline">{immersive ? 'Exit Full Screen' : 'Full Screen'}</span>
        </button>
      </div>

      {caps.manual_control && !stopped && (
        <p className={clsx('flex items-center gap-1.5 text-xs', dark ? 'text-white/80' : 'text-slate-400')}>
          {control.active ? (
            <>
              <MousePointer2 className="h-3.5 w-3.5 text-accent" />
              <span className="font-semibold text-accent">Interactive control ON</span>
              <span>· clicks and scrolling go to the real {session.integration}. The agent is holding off.</span>
            </>
          ) : lockedByOther ? (
            <span>{session.control?.owner} currently controls this session.</span>
          ) : !canControl ? (
            <span>Read-only login: view only.</span>
          ) : (
            <span>View-only. Taps on the screen do nothing until you press Enable Click.</span>
          )}
        </p>
      )}
      {control.note && <p className="text-xs font-medium text-amber-500">{control.note}</p>}

      {control.mode === 'full' && (
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex min-w-[200px] flex-1 items-center gap-2">
            <Keyboard className={clsx('h-4 w-4 shrink-0', dark ? 'text-white/70' : 'text-slate-400')} />
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && sendText()}
              maxLength={200}
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
              placeholder="Type into the focused field…"
              className="input min-h-[40px] flex-1"
            />
            <button type="button" onClick={sendText} className={clsx(base, idle)}>Send</button>
          </div>
          {['Enter', 'Tab', 'Backspace', 'Escape'].map((k) => (
            <button key={k} type="button" onClick={() => control.send({ type: 'key', key: k })} className={clsx(base, idle, '!min-h-[36px] !px-2')}>
              {k}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ------------------------------------------------------------------ verification banner
function VerificationBanner({ session }: { session: LiveSession }) {
  if (session.status !== 'waiting_for_user') return null
  return (
    <div className="flex gap-3 rounded-xl border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-amber-700">
      <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
      <div>
        <p className="font-semibold">The agent is paused and needs you</p>
        <p className="mt-0.5">{session.waiting?.reason || 'Human action is required.'}</p>
        <ol className="mt-1.5 list-decimal space-y-0.5 pl-4 text-xs">
          <li>Press Enable Click and complete the step yourself (only where the site permits manual completion).</li>
          <li>Disable Click to release control.</li>
          <li>Press Resume Agent. The page is re-checked first; the agent will not continue while verification is still showing.</li>
        </ol>
        {session.waiting?.timed_out && <p className="mt-1 text-xs font-semibold">No response for a long time. The session stays parked until you resume or stop it.</p>}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ details
function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-2 text-sm">
      <span className="shrink-0 text-slate-500">{label}</span>
      <span className="min-w-0 break-words text-right font-medium text-slate-200">{value}</span>
    </div>
  )
}

function Details({ session, stream }: { session: LiveSession; stream: { state: StreamState; fps: number } }) {
  const caps = session.capabilities
  const yes = (b: boolean) => (b ? 'Yes' : 'No')
  return (
    <div className="divide-y divide-surface-300/60">
      <Row label="Status" value={STATUS_LABEL[session.status]} />
      <Row label="Integration" value={session.integration} />
      {session.profile && <Row label="Profile" value={session.profile.name} />}
      {session.account && <Row label="Account" value={session.account} />}
      {session.task && <Row label="Task" value={`${session.task.id}${session.task.title ? ` · ${session.task.title}` : ''}`} />}
      {session.url && <Row label="Page" value={session.url} />}
      {session.viewport && <Row label="Viewport" value={`${session.viewport.width}×${session.viewport.height}`} />}
      <Row label="Started" value={new Date(session.created_at * 1000).toLocaleString()} />
      <Row label="Last activity" value={new Date(session.last_activity_at * 1000).toLocaleTimeString()} />
      {caps.visual && <Row label="Stream" value={`${stream.state}${stream.fps ? ` · ${stream.fps} fps` : ''}`} />}
      {typeof session.meta.saved_session_state === 'boolean' && <Row label="Saved cookies" value={yes(session.meta.saved_session_state)} />}
      {typeof session.meta.connected === 'boolean' && <Row label="Connected" value={yes(session.meta.connected)} />}
      {typeof session.meta.actions_today === 'number' && (
        <Row label="Actions today" value={`${session.meta.actions_today} / ${String(session.meta.daily_action_limit ?? '?')}`} />
      )}
      <Row label="Live screen" value={yes(caps.visual)} />
      <Row label="Manual control" value={yes(caps.manual_control)} />
      <Row label="Pause / resume" value={yes(caps.pause_resume)} />
    </div>
  )
}

function InfoLink({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-center gap-3 rounded-2xl bg-surface-200 px-3 py-2.5">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-surface-100 text-slate-400">{icon}</span>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-slate-500">{label}</p>
        <p className="truncate text-sm font-semibold text-slate-100">{value}</p>
      </div>
      <ChevronRight className="h-4 w-4 shrink-0 text-slate-500" />
    </div>
  )
}

// ------------------------------------------------------------------ page
export default function LiveSessionPage() {
  const { integration = '', key = '' } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const sid = `${integration}:${key}`
  const { overview, events } = useLiveFeed({ session: sid })
  const data = overview.data
  const session = data?.sessions.find((s) => s.id === sid)
  const { stale, ageSeconds } = useStale(overview)
  // Never act on a view that may be out of date, or while the worker is unreachable.
  const canControl = !!data?.can_control && !stale && !!data?.gateway.available
  const isDesktop = useMediaQuery('(min-width: 1024px)')

  const rootRef = useRef<HTMLDivElement>(null)
  const refreshRef = useRef<() => void>(() => undefined)
  const [immersive, setImmersive] = useState(false)
  const [drawer, setDrawer] = useState(false)
  const [tab, setTab] = useState<'activity' | 'details'>('activity')
  const [stream, setStream] = useState<{ state: StreamState; fps: number }>({ state: 'loading', fps: 0 })

  const control = useSessionControl(integration, key, () => window.setTimeout(() => refreshRef.current(), 250))
  const { enable } = control

  // "Enable Click" pressed on a card: turn it on here, once, as an explicit operator action.
  const autoDone = useRef(false)
  useEffect(() => {
    autoDone.current = false // a different session is a different explicit request
  }, [sid])
  const wantsClick = (location.state as { enableClick?: boolean } | null)?.enableClick === true
  useEffect(() => {
    if (!wantsClick || autoDone.current || !session || !canControl) return
    if (session.capabilities.manual_control && session.status !== 'stopped') {
      autoDone.current = true
      void enable('click')
    }
  }, [wantsClick, session, canControl, enable])

  const elapsed = useElapsed(session, overview.dataUpdatedAt || Date.now())

  const onState = useCallback((state: StreamState, fps: number) => setStream((p) => (p.state === state && p.fps === fps ? p : { state, fps })), [])
  const onRefreshHandle = useCallback((fn: () => void) => { refreshRef.current = fn }, [])

  const enterFullscreen = async () => {
    setImmersive(true)
    try { await rootRef.current?.requestFullscreen?.() } catch { /* the embedded full-viewport view is the fallback */ }
  }
  const exitFullscreen = async () => {
    setImmersive(false)
    try { if (document.fullscreenElement) await document.exitFullscreen() } catch { /* ignore */ }
  }
  useEffect(() => {
    const onChange = () => { if (!document.fullscreenElement) setImmersive(false) }
    document.addEventListener('fullscreenchange', onChange)
    return () => {
      document.removeEventListener('fullscreenchange', onChange)
      if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined)
    }
  }, [])

  const goBack = () => {
    void control.disable()
    navigate('/live')
  }

  // ---- states without a session
  const shell = (body: ReactNode) => (
    <div className="fixed inset-0 z-[60] flex flex-col items-center justify-center gap-3 bg-surface-100 p-6 text-center">
      {body}
      <button type="button" onClick={() => navigate('/live')} className="btn btn-primary">Back to Live Broadcast</button>
    </div>
  )
  if (overview.isLoading) return shell(<><Loader2 className="h-6 w-6 animate-spin text-slate-400" /><p className="text-slate-400">Loading session…</p></>)
  if (!data) return shell(<><WifiOff className="h-7 w-7 text-slate-400" /><p className="text-slate-300">Could not load live data.</p></>)
  if (!data.gateway.available) return shell(<><WifiOff className="h-7 w-7 text-amber-500" /><p className="max-w-sm text-slate-300">{data.gateway.reason}</p></>)
  if (!session) return shell(<><p className="text-lg font-semibold text-slate-100">Session not found</p><p className="max-w-sm text-sm text-slate-400">It may have ended or the worker restarted. Running sessions are listed on the Live Broadcast page.</p></>)

  const caps = session.capabilities
  const stopped = session.status === 'stopped'
  const interactive = control.active && canControl && !stopped
  const streaming = caps.visual && !stopped && stream.state === 'live'

  const screen = caps.visual ? (
    stopped ? (
      <div className="flex h-full w-full items-center justify-center text-sm text-slate-400">Session stopped</div>
    ) : (
      <LiveScreen
        integration={integration}
        sessionKey={key}
        quality="full"
        fit="contain"
        interactive={interactive}
        onInput={control.send}
        fallbackSize={session.viewport ?? undefined}
        onState={onState}
        onFrameRefreshRequested={onRefreshHandle}
      />
    )
  ) : (
    <div className="mx-auto h-full w-full max-w-2xl overflow-y-auto p-4">
      <p className="mb-3 rounded-lg bg-surface-200 p-3 text-sm text-slate-400">
        This is an API session (no graphical Telegram client), so there is no screen to show or click. Real activity events and metadata are listed below.
      </p>
      <ActivityList events={events} emptyText="No activity yet" />
    </div>
  )

  const staleBanner = stale ? (
    <p className="mb-1 rounded-lg bg-amber-500/90 px-3 py-1 text-center text-xs font-semibold text-white">
      Not refreshed for {ageSeconds}s: this view may be out of date and controls are disabled.
    </p>
  ) : null

  const activityPanel = (
    <div className="space-y-3">
      <ActivityList events={events} emptyText="No activity recorded for this session yet." />
    </div>
  )
  const panels = (
    <>
      <div className="mb-3 grid grid-cols-2 gap-1 rounded-xl bg-surface-200 p-1">
        {(['activity', 'details'] as const).map((t) => (
          <button key={t} type="button" onClick={() => setTab(t)} className={clsx('rounded-lg py-2 text-sm font-semibold capitalize', tab === t ? 'bg-accent text-white' : 'text-slate-400')}>
            {t === 'activity' ? 'Activity Log' : 'Details'}
          </button>
        ))}
      </div>
      {tab === 'activity' ? activityPanel : <Details session={session} stream={stream} />}
    </>
  )

  const titleBlock = (
    <div className="min-w-0">
      <p className="truncate text-base font-bold text-slate-100">{session.name}</p>
      <p className="truncate text-xs text-slate-500">{session.profile?.name ?? session.account ?? ''}</p>
    </div>
  )
  const statusLine = (
    <p className="flex items-center gap-1.5 text-xs text-slate-400">
      <span className={clsx('h-2 w-2 rounded-full', STATUS_DOT[session.status])} />
      {STATUS_LABEL[session.status]} · <span className="font-mono">{formatElapsed(elapsed)}</span>
    </p>
  )

  // ================================================================== desktop
  if (isDesktop) {
    return (
      <div ref={rootRef} className="fixed inset-0 z-[60] flex flex-col bg-surface-100">
        <header className="flex items-center gap-3 border-b border-surface-300 px-4 py-2.5">
          <button type="button" onClick={goBack} aria-label="Back to Live Broadcast" className="rounded-lg p-2 text-slate-400 hover:bg-surface-200">
            <ArrowLeft className="h-5 w-5" />
          </button>
          <IntegrationBadge id={session.integration} />
          {titleBlock}
          <StatusBadge status={session.status} streaming={streaming} />
          <span className="font-mono text-xs text-slate-500">{formatElapsed(elapsed)}</span>
          {session.url && (
            <span className="ml-2 hidden min-w-0 flex-1 items-center gap-1.5 truncate rounded-lg bg-surface-200 px-3 py-1.5 text-xs text-slate-400 xl:flex">
              <Link2 className="h-3.5 w-3.5 shrink-0" /> <span className="truncate">{session.url}</span>
            </span>
          )}
          <button type="button" onClick={() => setDrawer((d) => !d)} className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-surface-200 px-3 py-2 text-xs font-semibold text-slate-300 hover:bg-surface-300">
            {drawer ? <PanelRightClose className="h-4 w-4" /> : <PanelRightOpen className="h-4 w-4" />} Activity
          </button>
        </header>

        <div className="space-y-2 px-4 pt-3 empty:hidden"><VerificationBanner session={session} /></div>

        <div className="flex min-h-0 flex-1">
          <main className="flex min-w-0 flex-1 flex-col">
            <div className="min-h-0 flex-1 bg-slate-950/95 p-2">{staleBanner}{screen}</div>
            <div className="border-t border-surface-300 bg-surface-100 px-4 py-3">
              <ControlBar session={session} control={control} canControl={canControl} immersive={immersive} onToggleFullscreen={() => (immersive ? exitFullscreen() : enterFullscreen())} />
            </div>
          </main>
          {drawer && (
            <aside className="w-[340px] shrink-0 overflow-y-auto border-l border-surface-300 bg-surface-100 p-4">{panels}</aside>
          )}
        </div>
      </div>
    )
  }

  // ================================================================== phone, full screen
  if (immersive) {
    return (
      <div ref={rootRef} className="fixed inset-0 z-[60] flex h-dvh flex-col bg-slate-950">
        <div className="pt-[env(safe-area-inset-top)]">
          <div className="flex items-center gap-2 bg-black/50 px-3 py-1.5 text-white">
            <button type="button" onClick={exitFullscreen} aria-label="Exit full screen" className="rounded-lg p-2 hover:bg-white/15"><Minimize2 className="h-5 w-5" /></button>
            <span className="truncate text-sm font-bold">{session.name}</span>
            <StatusBadge status={session.status} streaming={streaming} />
            <button type="button" onClick={() => setDrawer((d) => !d)} aria-label="Activity log" className="ml-auto rounded-lg p-2 hover:bg-white/15"><ClipboardList className="h-5 w-5" /></button>
          </div>
        </div>
        <div className="min-h-0 flex-1 p-1">{staleBanner}{screen}</div>
        <div className="bg-black/60 px-3 pb-[calc(0.5rem+env(safe-area-inset-bottom))] pt-2 text-white">
          <ControlBar session={session} control={control} canControl={canControl} immersive onToggleFullscreen={exitFullscreen} dark />
        </div>
        {drawer && (
          <div className="absolute inset-x-0 bottom-0 z-10 max-h-[55dvh] overflow-y-auto rounded-t-2xl bg-surface-100 p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] shadow-nav">
            <div className="mb-2 flex items-center justify-between"><p className="font-bold text-slate-100">Activity</p><button type="button" onClick={() => setDrawer(false)} className="text-sm font-semibold text-accent">Close</button></div>
            {panels}
          </div>
        )}
      </div>
    )
  }

  // ================================================================== phone, detail screen
  return (
    <div ref={rootRef} className="fixed inset-0 z-[60] overflow-y-auto bg-surface-100 pb-[calc(1.5rem+env(safe-area-inset-bottom))]">
      <div className="pt-[env(safe-area-inset-top)]">
        <header className="flex items-center gap-2 px-4 py-3">
          <button type="button" onClick={goBack} aria-label="Back" className="-ml-2 rounded-lg p-2 text-slate-300"><ArrowLeft className="h-5 w-5" /></button>
          <div className="min-w-0 flex-1 text-center">
            <p className="truncate text-lg font-bold text-slate-100">{session.name}</p>
            <p className="truncate text-xs text-slate-500">{session.profile?.name ?? session.account ?? ''}</p>
            <div className="flex justify-center">{statusLine}</div>
          </div>
          <span className="w-9" />
        </header>
      </div>

      <div className="space-y-3 px-4">
        {staleBanner}
        <VerificationBanner session={session} />

        {caps.visual ? (
          <div className="relative overflow-hidden rounded-2xl bg-slate-900 p-1">
            {stopped ? (
              <div className="flex aspect-[16/10] items-center justify-center text-sm text-slate-400">Session stopped</div>
            ) : (
              <LiveScreen
                integration={integration}
                sessionKey={key}
                quality="full"
                fit="width"
                interactive={interactive}
                onInput={control.send}
                fallbackSize={session.viewport ?? undefined}
                onState={onState}
                onFrameRefreshRequested={onRefreshHandle}
              />
            )}
            <span className="absolute right-3 top-3"><StatusBadge status={session.status} streaming={streaming} /></span>
            <button type="button" onClick={enterFullscreen} aria-label="Open full screen" className="absolute bottom-3 right-3 flex h-10 w-10 items-center justify-center rounded-lg bg-black/60 text-white"><Maximize2 className="h-4 w-4" /></button>
          </div>
        ) : (
          <div className="rounded-2xl bg-surface-200 p-3 text-sm text-slate-400">
            API session: there is no graphical screen to show or click. Real activity is listed below.
          </div>
        )}

        <ControlBar session={session} control={control} canControl={canControl} immersive={false} onToggleFullscreen={enterFullscreen} />

        {session.profile && <InfoLink icon={<IntegrationBadge id={session.integration} size="sm" />} label="Browser Profile" value={session.profile.name} />}
        {session.url && <InfoLink icon={<Link2 className="h-4 w-4" />} label="Current URL" value={session.url} />}

        <div>{panels}</div>

        {caps.visual && !stopped && (
          <button type="button" onClick={enterFullscreen} className="flex min-h-[48px] w-full items-center justify-center gap-2 rounded-2xl bg-status-error text-sm font-bold text-white">
            <Maximize2 className="h-4 w-4" /> Open in Full Screen
          </button>
        )}
      </div>
    </div>
  )
}

