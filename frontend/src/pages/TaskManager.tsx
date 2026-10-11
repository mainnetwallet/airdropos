/** Task Manager: list, filter, inspect, cancel, and retry persistent tasks. */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Search, Trash2, XCircle } from 'lucide-react'
import { api } from '@/services/api'
import {
  Card,
  EmptyState,
  ErrorState,
  Loading,
  ProgressBar,
  StatusBadge,
} from '@/components/ui'
import type { TaskStatus } from '@/types'

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

export default function TaskManagerPage() {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'ALL'>('ALL')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  const tasksQuery = useQuery({
    queryKey: ['tasks', statusFilter],
    queryFn: () =>
      api.listTasks({ status: statusFilter === 'ALL' ? undefined : statusFilter, limit: 100 }),
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

  const filtered = (tasksQuery.data?.tasks ?? []).filter((t) =>
    search ? t.title.toLowerCase().includes(search.toLowerCase()) || t.task_id.includes(search) : true,
  )

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Task Manager</h1>
          <p className="mt-1 text-sm text-slate-400">
            Persistent tasks with AIR-YYYY-NNNNNN identifiers
          </p>
        </div>
        <div className="flex gap-2">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
            <input
              className="input pl-9 w-64"
              placeholder="Search tasks…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <button onClick={() => tasksQuery.refetch()} className="btn-ghost">
            <RefreshCw className="h-4 w-4" />
          </button>
        </div>
      </header>

      {/* Status filters */}
      <div className="flex flex-wrap gap-1.5">
        {STATUSES.map((status) => (
          <button
            key={status}
            onClick={() => setStatusFilter(status)}
            className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
              statusFilter === status
                ? 'bg-accent text-white'
                : 'bg-surface-200 text-slate-400 hover:bg-surface-300'
            }`}
          >
            {status.replace(/_/g, ' ')}
          </button>
        ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title={`Tasks (${filtered.length})`} className="lg:col-span-2">
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

        {/* Detail panel */}
        <Card title="Task Detail" className="lg:col-span-1">
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
                  className="btn-ghost flex-1"
                >
                  <RefreshCw className="h-4 w-4" />
                  Retry
                </button>
                <button
                  onClick={() => cancel.mutate(selected)}
                  disabled={cancel.isPending}
                  className="btn-danger flex-1"
                >
                  <XCircle className="h-4 w-4" />
                  Cancel
                </button>
                <button
                  onClick={() => handleDelete(selected)}
                  disabled={remove.isPending}
                  className="btn-danger flex-1"
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
  )
}
