/** Live Broadcast: every real running automation session on one screen (desktop grid + phone layout). */

import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { LayoutGrid, List, Plus, Radio, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { Alert, Card, Chips, EmptyState, ErrorState, Loading, PageHeader, type ChipItem } from '@/components/ui'
import SessionCard from '@/components/live/SessionCard'
import ActivityList from '@/components/live/ActivityList'
import NewSessionModal from '@/components/live/NewSessionModal'
import { IntegrationBadge, STATUS_DOT, STATUS_LABEL, STATUS_TILE } from '@/components/live/meta'
import { useLiveFeed, useStale } from '@/hooks/useLive'
import type { LiveResources, LiveSession, SessionStatus } from '@/types/live'

const STATUS_ORDER: SessionStatus[] = ['running', 'waiting_for_user', 'paused', 'failed', 'stopped']

function Resource({ label, value, unit = '%' }: { label: string; value: number | null | undefined; unit?: string }) {
  if (value == null) return null
  const pct = unit === '%' ? Math.min(100, value) : 0
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="w-16 text-slate-400">{label}</span>
      {unit === '%' && (
        <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-300">
          <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
        </div>
      )}
      <span className="ml-auto w-16 text-right font-semibold text-slate-200">
        {value}
        {unit === '%' ? '%' : ` ${unit}`}
      </span>
    </div>
  )
}

export default function LiveBroadcast() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const filter = params.get('f') ?? 'all'
  const { overview, events, socketConnected } = useLiveFeed()
  const { stale, ageSeconds } = useStale(overview)
  const [layout, setLayout] = useState<'grid' | 'list'>('grid')
  const [showNew, setShowNew] = useState(false)
  const [logCleared, setLogCleared] = useState(0)

  const data = overview.data
  const sessions = data?.sessions ?? []
  const visible = useMemo(
    () => (filter === 'all' ? sessions : sessions.filter((s) => s.integration === filter)),
    [sessions, filter],
  )
  const shownEvents = useMemo(
    () => events.filter((e) => new Date(e.at).getTime() > logCleared),
    [events, logCleared],
  )

  if (overview.isLoading) return <Loading label="Loading live sessions" />
  // A failed refresh with data already on screen keeps the data (marked stale) instead of blanking the page.
  if (!data) return <ErrorState error={overview.error} onRetry={() => overview.refetch()} />

  const open = (s: LiveSession, enableClick: boolean) =>
    navigate(`/live/${encodeURIComponent(s.integration)}/${encodeURIComponent(s.key)}`, { state: { enableClick } })

  const chips: Array<ChipItem<string>> = [
    { id: 'all', label: 'All', count: data.counts.total },
    ...data.integrations.map((i) => ({ id: i.id, label: i.label, count: i.session_count })),
  ]
  const activeIntegration = data.integrations.find((i) => i.id === filter)
  const fetchedAt = overview.dataUpdatedAt || Date.now()
  // Controls only act on what is on screen when that is current and the worker is reachable.
  const canControl = data.can_control && !stale && data.gateway.available
  const res = data.resources as Partial<LiveResources>
  const hasRes = [res.cpu_percent, res.ram_percent, res.disk_percent, res.network_kbps].some((v) => v != null)

  const newButton = (
    <button
      type="button"
      onClick={() => setShowNew(true)}
      disabled={!canControl || data.emergency_stop}
      title={data.emergency_stop ? 'Emergency stop is active' : !canControl ? 'Unavailable right now' : undefined}
      className="btn btn-primary hidden disabled:cursor-not-allowed disabled:opacity-50 lg:inline-flex"
    >
      <Plus className="h-4 w-4" /> New Session
    </button>
  )

  return (
    <div className="space-y-4 pt-1 lg:space-y-5 lg:pt-0">
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            <Radio className="h-6 w-6 text-status-error" /> Live Broadcast
          </span>
        }
        subtitle="Monitor all running automation sessions in real-time"
        action={newButton}
      />

      {stale && (
        <Alert kind="warning" title="Showing the last known state">
          The view has not refreshed for {ageSeconds}s, so it may be out of date. Controls are disabled until it updates.
        </Alert>
      )}
      {(data.degraded?.length ?? 0) > 0 && (
        <Alert kind="warning" title="Some sessions could not be listed">
          The worker could not read: {data.degraded?.join(', ')}. Sessions from those integrations are missing from this view, not necessarily stopped.
        </Alert>
      )}
      {!data.gateway.available && (
        <Alert kind="warning" title="No live data from the worker">
          {data.gateway.reason} Sessions live inside the worker process, so none can be shown until it is reachable.
        </Alert>
      )}
      {data.emergency_stop && (
        <Alert kind="error" title="Emergency stop is active">
          The agent cannot resume. You can still watch sessions and take manual control.
        </Alert>
      )}
      {!data.can_control && (
        <Alert kind="info">This login is read-only: you can watch sessions but not pause, stop or click.</Alert>
      )}

      {/* Status counts: pills on desktop, tiles on phones */}
      <div className="hidden flex-wrap gap-2 lg:flex">
        {STATUS_ORDER.map((s) => (
          <span key={s} className="inline-flex items-center gap-2 rounded-full bg-surface-200 px-3 py-1 text-xs font-semibold text-slate-300">
            <span className={clsx('h-2 w-2 rounded-full', STATUS_DOT[s])} />
            {data.counts[s]} {STATUS_LABEL[s]}
          </span>
        ))}
        <span className="ml-auto text-xs text-slate-500">{socketConnected ? 'Event stream connected' : 'Event stream reconnecting (polling)'}</span>
      </div>
      <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4 lg:hidden">
        {STATUS_ORDER.map((s) => (
          <div key={s} className={clsx('min-w-[92px] shrink-0 rounded-2xl px-3 py-2.5', STATUS_TILE[s])}>
            <p className="text-xl font-bold leading-none">{data.counts[s]}</p>
            <p className="mt-1 text-xs font-medium">{s === 'waiting_for_user' ? 'Waiting' : STATUS_LABEL[s]}</p>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <Chips
            items={chips}
            value={filter}
            onChange={(id) => setParams(id === 'all' ? {} : { f: id }, { replace: true })}
          />
        </div>
        <div className="hidden shrink-0 gap-1 rounded-lg bg-surface-200 p-1 sm:flex lg:hidden">
          {(['grid', 'list'] as const).map((l) => (
            <button
              key={l}
              type="button"
              aria-label={`${l} layout`}
              onClick={() => setLayout(l)}
              className={clsx('rounded-md p-1.5', layout === l ? 'bg-accent text-white' : 'text-slate-400')}
            >
              {l === 'grid' ? <LayoutGrid className="h-4 w-4" /> : <List className="h-4 w-4" />}
            </button>
          ))}
        </div>
      </div>

      {activeIntegration && !activeIntegration.available && (
        <Alert kind="info" title={`${activeIntegration.label}: no live sessions`}>
          {activeIntegration.note}
        </Alert>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0">
          {visible.length === 0 ? (
            <Card>
              <EmptyState
                title="No running sessions"
                description={
                  filter === 'all'
                    ? 'Start a browser session from a profile, or run an agent task that uses the browser or Telegram. Nothing is shown here unless it is really running.'
                    : 'No sessions for this integration right now.'
                }
                icon={<Radio className="h-8 w-8" />}
              />
            </Card>
          ) : (
            <div
              className={clsx(
                'grid gap-3 lg:gap-4',
                layout === 'list' ? 'grid-cols-1' : 'grid-cols-2 2xl:grid-cols-3',
              )}
            >
              {visible.map((s) => (
                <SessionCard
                  key={s.id}
                  session={s}
                  fetchedAt={fetchedAt}
                  canControl={canControl}
                  events={events}
                  onOpen={open}
                  dense
                />
              ))}
            </div>
          )}
        </div>

        <aside className="space-y-4">
          <Card
            title="Live Activity Log"
            action={
              <button
                type="button"
                onClick={() => setLogCleared(Date.now())}
                className="inline-flex items-center gap-1 rounded-lg bg-surface-200 px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
              >
                <Trash2 className="h-3 w-3" /> Clear
              </button>
            }
          >
            <ActivityList
              events={shownEvents}
              sessions={sessions}
              showSession
              limit={14}
              emptyText="No activity yet. Events appear here as sessions really act."
            />
          </Card>

          <Card title="Session Summary">
            <div className="grid grid-cols-3 gap-2">
              {data.integrations
                .filter((i) => i.available || i.session_count > 0)
                .map((i) => (
                  <div key={i.id} className="rounded-xl bg-surface-200 p-2.5 text-center">
                    <div className="mb-1 flex justify-center">
                      <IntegrationBadge id={i.id} size="sm" />
                    </div>
                    <p className="text-xl font-bold text-slate-100">
                      {sessions.filter((s) => s.integration === i.id && s.status === 'running').length}
                    </p>
                    <p className="text-[11px] text-slate-500">{i.label} running</p>
                  </div>
                ))}
            </div>
          </Card>

          <Card title="Resource Usage (worker host)">
            {hasRes ? (
              <div className="space-y-2.5">
                <Resource label={res.cpu_source === 'loadavg' ? 'CPU load' : 'CPU'} value={res.cpu_percent} />
                <Resource label="RAM" value={res.ram_percent} />
                <Resource label="Disk" value={res.disk_percent} />
                <Resource label="Network" value={res.network_kbps} unit="KB/s" />
              </div>
            ) : (
              <p className="text-sm text-slate-500">Not measurable here (worker offline or unsupported platform).</p>
            )}
          </Card>
        </aside>
      </div>

      {/* Phone: floating New Session above the bottom navigation */}
      <button
        type="button"
        onClick={() => setShowNew(true)}
        className="btn btn-primary fixed bottom-[calc(5.75rem+env(safe-area-inset-bottom))] right-4 z-30 rounded-full px-5 py-3 shadow-lg lg:hidden"
      >
        <Plus className="h-4 w-4" /> New Session
      </button>

      {showNew && (
        <NewSessionModal
          integrations={data.integrations}
          onClose={() => setShowNew(false)}
          onCreated={(s) => {
            setShowNew(false)
            open(s, false)
          }}
        />
      )}
    </div>
  )
}
