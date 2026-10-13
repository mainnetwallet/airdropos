/**
 * Home / Mission Control.
 *  - Mobile and tablet: dark hero, stat cards and a live activity list (all real data).
 *  - Desktop: the original Mission Control dashboard.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  AlertTriangle,
  Bell,
  Bot,
  CalendarClock,
  CheckCircle2,
  Compass,
  Flame,
  Info,
  Layers,
  Radio,
  Wallet,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import clsx from 'clsx'
import { formatDistanceToNow } from 'date-fns'
import { api } from '@/services/api'
import {
  Card,
  ErrorState,
  HealthDot,
  IconTile,
  Loading,
  SeverityBadge,
  Stat,
} from '@/components/ui'
import type { DiagnosticsResponse, Overview as OverviewData, WorkerStatus } from '@/types'

function ago(iso?: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : formatDistanceToNow(date, { addSuffix: true })
}

function greeting(): string {
  const hour = new Date().getHours()
  if (hour < 5) return 'Good evening'
  if (hour < 12) return 'Good morning'
  if (hour < 18) return 'Good afternoon'
  return 'Good evening'
}

// ==================================================================== Mobile
interface ActivityRow {
  key: string
  icon: LucideIcon
  tone: 'blue' | 'green' | 'orange' | 'red' | 'purple'
  title: string
  subtitle: string
  time: string
}

function buildActivity(data: OverviewData | undefined, worker: WorkerStatus | undefined): ActivityRow[] {
  const rows: ActivityRow[] = []

  const current = worker?.worker.current_task
  if (current) {
    rows.push({
      key: `current-${current.task_id}`,
      icon: Radio,
      tone: 'green',
      title: current.title,
      subtitle: `Running · ${current.task_id}`,
      time: ago(current.since),
    })
  }

  const last = worker?.worker.last_task
  if (last) {
    rows.push({
      key: `last-${last.task_id}-${last.at}`,
      icon: CheckCircle2,
      tone: 'blue',
      title: `Finished ${last.task_id}`,
      subtitle: `Outcome: ${last.outcome}`,
      time: ago(last.at),
    })
  }

  for (const [i, n] of (data?.recent_notifications ?? []).slice(0, 3).entries()) {
    const failing = n.severity === 'error' || n.severity === 'critical'
    rows.push({
      key: `note-${i}-${n.sent_at}`,
      icon: failing || n.severity === 'warning' ? AlertTriangle : n.severity === 'success' ? CheckCircle2 : Info,
      tone: failing ? 'red' : n.severity === 'warning' ? 'orange' : n.severity === 'success' ? 'green' : 'blue',
      title: n.title,
      subtitle: n.body,
      time: ago(n.sent_at),
    })
  }

  return rows.slice(0, 5)
}

function MobileHome({
  data,
  worker,
  error,
  onRetry,
}: {
  data: OverviewData | undefined
  worker: WorkerStatus | undefined
  error: unknown
  onRetry: () => void
}) {
  const alive = worker?.alive === true
  const statusLabel = worker === undefined ? 'Checking worker' : alive ? 'AI Agent Running' : 'Worker offline'
  const stats: Array<{ label: string; value: number | undefined; to: string; icon: LucideIcon; tone: 'green' | 'purple' | 'orange' }> = [
    { label: 'Airdrops Tracking', value: data?.campaigns, to: '/airdrops', icon: Compass, tone: 'green' },
    { label: 'Active Tasks', value: data?.active_tasks, to: '/tasks', icon: Layers, tone: 'purple' },
    { label: 'Wallets Connected', value: data?.wallets, to: '/wallets', icon: Wallet, tone: 'green' },
    // A task needing a human is resolved in the Agent Inbox, not by filtering a list here.
    { label: 'Waiting For You', value: data?.needs_user_action, to: '/inbox', icon: Bell, tone: 'orange' },
  ]
  const activity = buildActivity(data, worker)

  return (
    <div className="lg:hidden">
      {/* Hero */}
      <section
        className="relative overflow-hidden pb-20 pt-[calc(env(safe-area-inset-top,0px)+4.5rem)] text-white"
        style={{
          background:
            'radial-gradient(120% 70% at 85% 0%, rgba(99,102,241,.55), transparent 60%),' +
            'radial-gradient(90% 60% at 0% 100%, rgba(14,165,233,.35), transparent 60%),' +
            'linear-gradient(180deg,#0a1226 0%,#101b3a 65%,#18264d 100%)',
        }}
      >
        <div className="mx-auto max-w-3xl px-5">
          <Link
            to="/worker"
            className={clsx(
              'inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium backdrop-blur',
              alive ? 'border-emerald-400/40 bg-emerald-400/15 text-emerald-100' : 'border-white/25 bg-white/10 text-white/90',
            )}
          >
            <span
              className={clsx(
                'h-2 w-2 rounded-full',
                worker === undefined ? 'bg-slate-300' : alive ? 'animate-pulse bg-emerald-400' : 'bg-red-400',
              )}
            />
            {statusLabel}
          </Link>
          <h1 className="mt-4 text-[34px] font-bold leading-tight tracking-tight">{greeting()}</h1>
          <p className="mt-1 max-w-[16rem] text-[15px] leading-snug text-white/80">
            {worker !== undefined && !alive
              ? 'The worker is offline, so queued tasks will not run.'
              : 'Your airdrop agent is working for you.'}
          </p>
        </div>
      </section>

      <div className="mx-auto -mt-14 max-w-3xl space-y-5 px-4">
        {/* Stat cards */}
        <div className="grid grid-cols-4 gap-2">
          {stats.map(({ label, value, to, icon: Icon, tone }) => (
            <Link
              key={label}
              to={to}
              className="flex flex-col items-center rounded-2xl border border-surface-300/60 bg-surface-100 px-1 pb-2.5 pt-3 text-center shadow-card"
            >
              <IconTile tone={tone} className="h-10 w-10">
                <Icon />
              </IconTile>
              <span className="mt-1.5 text-2xl font-bold tabular-nums leading-none text-slate-100">
                {value ?? '—'}
              </span>
              <span className="mt-1 text-[10px] leading-tight text-slate-500">{label}</span>
            </Link>
          ))}
        </div>

        {Boolean(error) && !data && <ErrorState error={error} onRetry={onRetry} />}

        {/* Current activity */}
        <section className="card" aria-labelledby="current-activity">
          <header className="mb-3 flex items-center justify-between">
            <h2 id="current-activity" className="text-lg font-bold text-slate-100">
              Current Activity
            </h2>
            <span
              className={clsx(
                'inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold',
                alive ? 'bg-orange-500/15 text-orange-500' : 'bg-slate-500/15 text-slate-400',
              )}
            >
              <Flame className="h-3 w-3" />
              {alive ? 'Live' : 'Idle'}
            </span>
          </header>

          {activity.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">
              No activity yet. Ask the agent to start a task from Chat.
            </p>
          ) : (
            <ul className="divide-y divide-surface-300/70">
              {activity.map(({ key, icon: Icon, tone, title, subtitle, time }) => (
                <li key={key} className="flex items-center gap-3 py-3 first:pt-0 last:pb-0">
                  <IconTile tone={tone}>
                    <Icon />
                  </IconTile>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[15px] font-semibold text-slate-100">{title}</p>
                    <p className="truncate text-xs text-slate-500">{subtitle}</p>
                  </div>
                  {time && <span className="shrink-0 text-[11px] text-slate-500">{time}</span>}
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* Deadlines (only when there are some) */}
        {data && data.upcoming_deadlines.length > 0 && (
          <section className="card">
            <h2 className="mb-3 text-lg font-bold text-slate-100">Upcoming Deadlines</h2>
            <ul className="space-y-2">
              {data.upcoming_deadlines.map((d) => (
                <li key={d.task_id} className="flex items-center justify-between gap-3 rounded-xl bg-surface-200 px-3 py-2.5">
                  <p className="truncate text-sm text-slate-200">{d.title}</p>
                  <span className="flex shrink-0 items-center gap-1 text-xs text-status-warning">
                    <CalendarClock className="h-3.5 w-3.5" />
                    {ago(d.deadline)}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  )
}

// ==================================================================== Desktop
function DesktopOverview({
  data,
  diagnostics,
  isLoading,
  error,
  refetch,
}: {
  data: OverviewData | undefined
  diagnostics: DiagnosticsResponse | undefined
  isLoading: boolean
  error: unknown
  refetch: () => void
}) {
  if (isLoading) return <Loading label="Loading mission control" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />
  if (!data) return null

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Mission Control</h1>
          <p className="mt-1 text-sm text-slate-400">
            Autonomous agent platform status and activity
          </p>
        </div>
        <div className="flex items-center gap-2 rounded-lg border border-surface-300 bg-surface-100 px-3 py-2">
          <Bot className="h-4 w-4 text-accent" />
          <div className="text-xs">
            <p className="font-mono text-slate-200">{data.agent.model_id}</p>
            <p className="text-slate-500">Single fixed model</p>
          </div>
        </div>
      </header>

      {/* Primary metrics */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Active Tasks" value={data.active_tasks} tone="info" />
        <Stat label="Completed" value={data.completed_tasks} tone="success" />
        <Stat label="Failed" value={data.failed_tasks} tone={data.failed_tasks > 0 ? 'error' : 'neutral'} />
        <Stat
          label="Needs Your Action"
          value={data.needs_user_action}
          tone={data.needs_user_action > 0 ? 'warning' : 'neutral'}
        />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Projects" value={data.projects} />
        <Stat label="Campaigns" value={data.campaigns} />
        <Stat label="Wallets" value={data.wallets} />
        <Stat label="Tracked Cost" value={`$${data.tracked_cost_usd.toFixed(2)}`} tone="warning" />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Deadlines */}
        <Card
          title="Upcoming Deadlines"
          className="lg:col-span-2"
          action={
            <Link to="/tasks" className="text-xs text-accent hover:underline">
              All tasks →
            </Link>
          }
        >
          {data.upcoming_deadlines.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">No upcoming deadlines.</p>
          ) : (
            <ul className="space-y-2">
              {data.upcoming_deadlines.map((d) => (
                <li
                  key={d.task_id}
                  className="flex items-center justify-between rounded-lg bg-surface-200 px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm text-slate-200">{d.title}</p>
                    <p className="font-mono text-xs text-slate-500">{d.task_id}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1 text-xs text-status-warning">
                    <CalendarClock className="h-3.5 w-3.5" />
                    {formatDistanceToNow(new Date(d.deadline), { addSuffix: true })}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* Subsystem health */}
        <Card title="Subsystem Health">
          {diagnostics ? (
            <ul className="space-y-2.5">
              {Object.entries(diagnostics.subsystems).map(([name, info]) => (
                <li
                  key={name}
                  className="flex items-center justify-between rounded-lg bg-surface-200 px-3 py-2"
                >
                  <span className="truncate text-sm text-slate-300">{name}</span>
                  <HealthDot status={String(info.status)} />
                </li>
              ))}
            </ul>
          ) : (
            <div className="space-y-2">
              {[...Array(5)].map((_, i) => (
                <div key={i} className="skeleton h-9 rounded-lg" />
              ))}
            </div>
          )}
        </Card>
      </div>

      {/* Notifications */}
      <Card
        title="Recent Notifications"
        action={
          <span className="flex items-center gap-1 text-xs text-slate-500">
            <Flame className="h-3.5 w-3.5" /> deduped
          </span>
        }
      >
        {data.recent_notifications.length === 0 ? (
          <p className="py-6 text-center text-sm text-slate-500">No notifications yet.</p>
        ) : (
          <ul className="divide-y divide-surface-200">
            {data.recent_notifications.map((n, i) => (
              <li key={i} className="flex items-start justify-between gap-4 py-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-200">{n.title}</p>
                  <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">{n.body}</p>
                </div>
                <SeverityBadge severity={n.severity} />
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

// ==================================================================== Page
export default function Overview() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 10000,
  })

  const { data: diagnostics } = useQuery({
    queryKey: ['diagnostics'],
    queryFn: api.diagnostics,
    refetchInterval: 30000,
  })

  // Same key as the layout's sidebar indicator, so this shares its request.
  const { data: worker } = useQuery({
    queryKey: ['worker-status-mini'],
    queryFn: api.workerStatus,
    refetchInterval: 5000,
    retry: false,
  })

  return (
    <>
      <MobileHome data={data} worker={worker} error={error} onRetry={() => refetch()} />
      <div className="hidden lg:block">
        <DesktopOverview
          data={data}
          diagnostics={diagnostics}
          isLoading={isLoading}
          error={error}
          refetch={() => refetch()}
        />
      </div>
    </>
  )
}
