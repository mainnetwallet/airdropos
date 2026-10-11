/** Task Manager: list, filter, inspect, cancel, and retry persistent tasks. */

import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  MoreVertical,
  Radio,
  RefreshCw,
  Search,
  Trash2,
  X,
  XCircle,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import clsx from 'clsx'
import { formatDistanceToNow } from 'date-fns'
import { api } from '@/services/api'
import {
  Alert,
  Card,
  Chips,
  EmptyState,
  ErrorState,
  IconTile,
  Loading,
  PageHeader,
  ProgressBar,
  StatusBadge,
} from '@/components/ui'
import type { TaskStatus, TaskSummary } from '@/types'

const STATUSES: Array<TaskStatus | 'ALL'> = [
  'ALL',
  'IN_PROGRESS',
  'READY',
  'RETRYING',
  'NEEDS_USER_ACTION',
  'COMPLETED',
  'FAILED',
  'UNVERIFIED',
  'PARTIALLY_COMPLETED',
]

// Phone filter groups, built from the real task statuses.
type Group = 'all' | 'running' | 'pending' | 'completed' | 'failed'
const GROUP_STATUSES: Record<Exclude<Group, 'all'>, TaskStatus[]> = {
  running: ['IN_PROGRESS', 'RESEARCHING', 'PLANNING', 'RETRYING'],
  pending: ['CREATED', 'READY', 'WAITING', 'NEEDS_USER_ACTION', 'UNVERIFIED'],
  completed: ['COMPLETED', 'PARTIALLY_COMPLETED'],
  failed: ['FAILED', 'CANCELLED'],
}
const GROUP_LABELS: Record<Group, string> = {
  all: 'All',
  running: 'Running',
  pending: 'Pending',
  completed: 'Completed',
  failed: 'Failed',
}

function inGroup(task: TaskSummary, group: Group): boolean {
  return group === 'all' || GROUP_STATUSES[group].includes(task.status)
}

function groupForStatus(status: TaskStatus | 'ALL'): Group {
  if (status === 'ALL') return 'all'
  const found = (Object.keys(GROUP_STATUSES) as Array<Exclude<Group, 'all'>>).find((g) =>
    GROUP_STATUSES[g].includes(status),
  )
  return found ?? 'all'
}

const STATUS_ICON: Record<string, { icon: LucideIcon; tone: 'blue' | 'green' | 'orange' | 'red' | 'purple' | 'slate' }> = {
  IN_PROGRESS: { icon: Radio, tone: 'blue' },
  RESEARCHING: { icon: Radio, tone: 'blue' },
  PLANNING: { icon: Radio, tone: 'blue' },
  RETRYING: { icon: RefreshCw, tone: 'purple' },
  COMPLETED: { icon: CheckCircle2, tone: 'green' },
  PARTIALLY_COMPLETED: { icon: CheckCircle2, tone: 'green' },
  NEEDS_USER_ACTION: { icon: AlertTriangle, tone: 'orange' },
  UNVERIFIED: { icon: AlertTriangle, tone: 'orange' },
  FAILED: { icon: XCircle, tone: 'red' },
  CANCELLED: { icon: XCircle, tone: 'slate' },
}

function updatedAgo(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : formatDistanceToNow(date, { addSuffix: true })
}

export default function TaskManagerPage() {
  const queryClient = useQueryClient()
  const [params] = useSearchParams()

  // The header bell links here with ?status=NEEDS_USER_ACTION.
  const initialStatus = (() => {
    const wanted = params.get('status') as TaskStatus | null
    return wanted && STATUSES.includes(wanted) ? wanted : 'ALL'
  })()

  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'ALL'>(initialStatus)
  const [group, setGroup] = useState<Group>(groupForStatus(initialStatus))
  const [search, setSearch] = useState('')
  const [showSearch, setShowSearch] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)

  const tasksQuery = useQuery({
    queryKey: ['tasks', statusFilter],
    queryFn: () =>
      api.listTasks({ status: statusFilter === 'ALL' ? undefined : statusFilter, limit: 100 }),
    refetchInterval: 8000,
  })

  // Unfiltered list: the phone's filter chips show real counts from it.
  const allQuery = useQuery({
    queryKey: ['tasks', 'ALL'],
    queryFn: () => api.listTasks({ limit: 100 }),
    refetchInterval: 8000,
  })

  const detailQuery = useQuery({
    queryKey: ['task', selected],
    queryFn: () => api.getTask(selected!),
    enabled: Boolean(selected),
  })

  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelTask(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['task', selected] })
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteTask(id),
    onSuccess: () => {
      setSelected(null)
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })

  const handleDelete = (id: string) => {
    if (window.confirm(`Delete task ${id} permanently? This cannot be undone.`)) {
      remove.mutate(id)
    }
  }

  const retry = useMutation({
    mutationFn: (id: string) => api.retryTask(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['task', selected] })
    },
  })

  const matchesSearch = (t: TaskSummary) =>
    search ? t.title.toLowerCase().includes(search.toLowerCase()) || t.task_id.includes(search) : true

  const filtered = (tasksQuery.data?.tasks ?? []).filter(matchesSearch)

  // Phone list: the same data, narrowed by the chosen group.
  const allTasks = allQuery.data?.tasks ?? []
  const phoneSource = statusFilter === 'ALL' ? allTasks : (tasksQuery.data?.tasks ?? [])
  const phoneList = phoneSource.filter((t) => inGroup(t, group)).filter(matchesSearch)

  const chooseGroup = (next: Group) => {
    setStatusFilter('ALL')
    setGroup(next)
  }
  const chooseStatus = (next: TaskStatus | 'ALL') => {
    setStatusFilter(next)
    setGroup(groupForStatus(next))
  }

  const groupChips = (Object.keys(GROUP_LABELS) as Group[]).map((g) => ({
    id: g,
    label: GROUP_LABELS[g],
    count: allQuery.data ? allTasks.filter((t) => inGroup(t, g)).length : undefined,
  }))

  const phoneLoading = statusFilter === 'ALL' ? allQuery.isLoading : tasksQuery.isLoading
  const phoneError = statusFilter === 'ALL' ? allQuery.error : tasksQuery.error

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title="Task Manager"
        subtitle={
          <>
            <span className="lg:hidden">Manage and monitor all tasks</span>
            <span className="hidden lg:inline">Persistent tasks with AIR-YYYY-NNNNNN identifiers</span>
          </>
        }
        action={
          <>
            {/* Desktop: search field + refresh */}
            <div className="relative hidden lg:block">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
              <input
                className="input w-64 pl-9"
                placeholder="Search tasks…"
                aria-label="Search tasks"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            {/* Phone: icon buttons */}
            <button
              type="button"
              onClick={() => setShowSearch((v) => !v)}
              aria-label="Search tasks"
              aria-pressed={showSearch}
              className="flex h-11 w-11 items-center justify-center rounded-xl border border-surface-300 bg-surface-100 text-slate-200 shadow-card lg:hidden"
            >
              <Search className="h-5 w-5" />
            </button>
            <button
              type="button"
              onClick={() => {
                void tasksQuery.refetch()
                void allQuery.refetch()
              }}
              aria-label="Refresh tasks"
              className="flex h-11 w-11 items-center justify-center rounded-xl border border-surface-300 bg-surface-100 text-slate-200 shadow-card lg:btn-ghost lg:h-auto lg:w-auto lg:rounded-lg lg:border-0 lg:shadow-none"
            >
              <RefreshCw className="h-5 w-5 lg:h-4 lg:w-4" />
            </button>
          </>
        }
      />

      {showSearch && (
        <input
          className="input lg:hidden"
          placeholder="Search tasks…"
          aria-label="Search tasks"
          autoFocus
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      )}

      {/* Filters: phone groups, desktop statuses */}
      <Chips className="lg:hidden" items={groupChips} value={group} onChange={chooseGroup} />
      <Chips
        className="hidden lg:flex"
        items={STATUSES.map((s) => ({ id: s, label: s.replace(/_/g, ' ') }))}
        value={statusFilter}
        onChange={chooseStatus}
      />

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Phone list */}
        <div className="space-y-3 lg:hidden">
          {phoneLoading && <Loading label="Loading tasks" />}
          {phoneError && <ErrorState error={phoneError} onRetry={() => void allQuery.refetch()} />}
          {!phoneLoading && !phoneError && phoneList.length === 0 && (
            <EmptyState
              title="No tasks"
              description="Create one from Chat, or via the Telegram bot."
            />
          )}
          {phoneList.map((task) => {
            const visual = STATUS_ICON[task.status] ?? { icon: Clock, tone: 'slate' as const }
            const Icon = visual.icon
            return (
              <div
                key={task.task_id}
                role="button"
                tabIndex={0}
                onClick={() => setSelected(task.task_id)}
                onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setSelected(task.task_id)}
                className={clsx(
                  'flex items-center gap-3 rounded-2xl border bg-surface-100 p-3 shadow-card',
                  selected === task.task_id ? 'border-accent' : 'border-surface-300/60',
                )}
              >
                <IconTile tone={visual.tone} className="h-12 w-12">
                  <Icon />
                </IconTile>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[15px] font-semibold text-slate-100">{task.title}</p>
                  <p className="truncate text-xs text-slate-500">
                    {[task.network, updatedAgo(task.updated_at)].filter(Boolean).join(' · ')}
                  </p>
                  {task.progress > 0 && task.progress < 1 && <ProgressBar value={task.progress} className="mt-2" />}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <StatusBadge status={task.status} />
                  <MoreVertical className="h-5 w-5 text-slate-500" aria-hidden />
                </div>
              </div>
            )
          })}
        </div>

        {/* Desktop table */}
        <Card title={`Tasks (${filtered.length})`} className="hidden lg:col-span-2 lg:block">
          {tasksQuery.isLoading && <Loading label="Loading tasks" />}
          {tasksQuery.error && (
            <ErrorState error={tasksQuery.error} onRetry={() => tasksQuery.refetch()} />
          )}
          {!tasksQuery.isLoading && filtered.length === 0 && (
            <EmptyState
              title="No tasks"
              description="Create one from the AI Agent Console, or via the Telegram bot."
            />
          )}

          {filtered.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr>
                    <th className="table-header">Task ID</th>
                    <th className="table-header">Title</th>
                    <th className="table-header">Status</th>
                    <th className="table-header">Progress</th>
                    <th className="table-header">Retries</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((task) => (
                    <tr
                      key={task.task_id}
                      onClick={() => setSelected(task.task_id)}
                      className={`table-row cursor-pointer ${
                        selected === task.task_id ? 'bg-accent/10' : ''
                      }`}
                    >
                      <td className="font-mono text-xs text-slate-400">{task.task_id}</td>
                      <td className="max-w-xs truncate text-sm text-slate-200">{task.title}</td>
                      <td>
                        <StatusBadge status={task.status} />
                      </td>
                      <td className="w-32">
                        <ProgressBar value={task.progress} />
                        <span className="text-[10px] text-slate-500">
                          {Math.round(task.progress * 100)}%
                        </span>
                      </td>
                      <td className="text-xs text-slate-400">{task.retry_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* Detail: bottom sheet on phones, side panel on desktop */}
        <div
          className={clsx(
            'lg:col-span-1',
            selected
              ? 'fixed inset-0 z-[60] flex items-end bg-black/50 lg:static lg:z-auto lg:block lg:bg-transparent'
              : 'hidden lg:block',
          )}
          onClick={() => setSelected(null)}
        >
          <div
            className="max-h-[85dvh] w-full overflow-y-auto rounded-t-3xl bg-surface-50 pb-[env(safe-area-inset-bottom)] lg:max-h-none lg:overflow-visible lg:rounded-none lg:bg-transparent lg:pb-0"
            onClick={(e) => e.stopPropagation()}
          >
            <Card
              title="Task Detail"
              className="max-lg:rounded-b-none max-lg:shadow-none"
              action={
                selected ? (
                  <button
                    type="button"
                    onClick={() => setSelected(null)}
                    aria-label="Close task detail"
                    className="flex h-10 w-10 items-center justify-center rounded-full text-slate-400 hover:bg-surface-200 lg:hidden"
                  >
                    <X className="h-5 w-5" />
                  </button>
                ) : undefined
              }
            >
              {!selected && (
                <EmptyState title="No task selected" description="Select a task to inspect its record." />
              )}
              {selected && detailQuery.isLoading && <Loading label="Loading task" />}
              {selected && detailQuery.error && <ErrorState error={detailQuery.error} />}

              {selected && detailQuery.data && (
                <div className="space-y-4">
                  <div>
                    <p className="font-mono text-xs text-slate-500">{detailQuery.data.task_id}</p>
                    <h3 className="mt-0.5 font-medium text-slate-100">{detailQuery.data.title}</h3>
                    <div className="mt-2">
                      <StatusBadge status={detailQuery.data.status} />
                    </div>
                  </div>

                  {detailQuery.data.status === 'NEEDS_USER_ACTION' && (
                    <Alert kind="warning" title="Operator action needed">
                      <p>
                        {detailQuery.data.blocker ??
                          'The agent stopped because a human step is required.'}
                      </p>
                      {detailQuery.data.next_planned_action && (
                        <p className="mt-1 font-medium">
                          Next: {detailQuery.data.next_planned_action}
                        </p>
                      )}
                      <p className="mt-1 text-[11px] opacity-80">
                        Do the step yourself, then press Retry to let the agent continue.
                      </p>
                    </Alert>
                  )}

                  {detailQuery.data.status !== 'NEEDS_USER_ACTION' &&
                    detailQuery.data.next_planned_action && (
                      <p className="text-xs text-slate-400">
                        <span className="font-semibold uppercase text-slate-500">Next: </span>
                        {detailQuery.data.next_planned_action}
                      </p>
                    )}

                  <div className="grid grid-cols-2 gap-2 text-center">
                    <div className="rounded-lg bg-surface-200 p-2">
                      <p className="text-lg font-bold text-slate-100">{detailQuery.data.evidence_count}</p>
                      <p className="text-[10px] uppercase text-slate-500">Evidence</p>
                    </div>
                    <div className="rounded-lg bg-surface-200 p-2">
                      <p className="text-lg font-bold text-slate-100">{detailQuery.data.checkpoints}</p>
                      <p className="text-[10px] uppercase text-slate-500">Checkpoints</p>
                    </div>
                  </div>

                  <div>
                    <p className="mb-1 text-xs font-semibold uppercase text-slate-500">
                      Pending actions ({detailQuery.data.pending_actions.length})
                    </p>
                    {detailQuery.data.pending_actions.length === 0 ? (
                      <p className="text-xs text-slate-500">None outstanding.</p>
                    ) : (
                      <ul className="space-y-1">
                        {detailQuery.data.pending_actions.map((a, i) => (
                          <li key={i} className="truncate text-xs text-slate-400">
                            • {String(a.description ?? JSON.stringify(a))}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>

                  {detailQuery.data.recent_events.length > 0 && (
                    <div>
                      <p className="mb-1 text-xs font-semibold uppercase text-slate-500">Activity</p>
                      <ul className="space-y-1.5">
                        {detailQuery.data.recent_events.slice(0, 6).map((ev, i) => (
                          <li key={i} className="text-xs text-slate-400">
                            <span className="text-slate-500">
                              {new Date(ev.at).toLocaleTimeString()}{' '}
                            </span>
                            <span className="text-slate-300">{ev.type.replace(/_/g, ' ')}</span>
                            {ev.reason && <span className="block text-slate-500">{ev.reason}</span>}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {detailQuery.data.transactions.length > 0 && (
                    <div>
                      <p className="mb-1 text-xs font-semibold uppercase text-slate-500">Transactions</p>
                      <ul className="space-y-1">
                        {detailQuery.data.transactions.map((tx, i) => (
                          <li key={i} className="flex justify-between text-xs">
                            <span className="font-mono text-slate-400">
                              {tx.hash?.slice(0, 12) ?? 'pending'}
                            </span>
                            <span className={tx.status === 'CONFIRMED' ? 'text-status-success' : 'text-status-warning'}>
                              {tx.status} ({tx.confirmations})
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <div className="flex gap-2 border-t border-surface-300 pt-3">
                    <button
                      onClick={() => retry.mutate(selected)}
                      disabled={retry.isPending}
                      className="btn-ghost flex-1 justify-center"
                    >
                      <RefreshCw className="h-4 w-4" />
                      Retry
                    </button>
                    <button
                      onClick={() => cancel.mutate(selected)}
                      disabled={cancel.isPending}
                      className="btn-danger flex-1 justify-center"
                    >
                      <XCircle className="h-4 w-4" />
                      Cancel
                    </button>
                    <button
                      onClick={() => handleDelete(selected)}
                      disabled={remove.isPending}
                      className="btn-danger flex-1 justify-center"
                    >
                      <Trash2 className="h-4 w-4" />
                      Delete
                    </button>
                  </div>
                  {remove.error && (
                    <p className="text-xs text-status-error">{(remove.error as Error).message}</p>
                  )}
                </div>
              )}
            </Card>
          </div>
        </div>
      </div>
    </div>
  )
}
