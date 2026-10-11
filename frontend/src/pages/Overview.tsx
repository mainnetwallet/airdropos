/** Mission Control: platform-wide status and key metrics. */

import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Bot, CalendarClock, Flame } from 'lucide-react'
import { api } from '@/services/api'
import { Card, ErrorState, HealthDot, Loading, SeverityBadge, Stat } from '@/components/ui'
import { formatDistanceToNow } from 'date-fns'

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
