/** Remaining dashboard pages, grouped to keep the router simple. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  Brain,
  Compass,
  GitBranch,
  LifeBuoy,
  Radio,
  Target,
  Zap,
} from 'lucide-react'
import { api } from '@/services/api'
import { Card, EmptyState, ErrorState, Loading, ProgressBar } from '@/components/ui'

// ==================================================================== Goals
export function Goals() {
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ['goals'], queryFn: api.listGoals })
  const queryClient = useQueryClient()
  const pause = useMutation({
    mutationFn: (id: string) => api.pauseGoal(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['goals'] }),
  })
  const resume = useMutation({
    mutationFn: (id: string) => api.resumeGoal(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['goals'] }),
  })

  if (isLoading) return <Loading label="Loading goals" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />

  const goals = data?.goals ?? []
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-slate-100">Goals & Strategy</h1>
        <p className="mt-1 text-sm text-slate-400">Persistent long-term objectives</p>
      </header>
      {goals.length === 0 ? (
        <EmptyState
          title="No goals defined"
          description="Ask the agent to define a long-term goal, e.g. 'Continuously discover and complete legitimate airdrops.'"
          icon={<Target className="h-8 w-8" />}
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {goals.map((goal) => (
            <Card key={goal.id}>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-mono text-xs text-slate-500">{goal.goal_code}</p>
                  <h3 className="mt-0.5 font-medium text-slate-100">{goal.title}</h3>
                </div>
                <span className={`badge shrink-0 ${goal.status === 'ACTIVE' ? 'bg-green-900 text-green-300' : 'bg-slate-800 text-slate-400'}`}>
                  {goal.status}
                </span>
              </div>
              <ProgressBar value={goal.progress} className="mt-3" />
              <p className="mt-1 text-xs text-slate-500">{Math.round(goal.progress * 100)}% complete</p>
              {goal.next_planned_action && (
                <p className="mt-3 text-xs text-slate-400">
                  <span className="text-slate-500">Next:</span> {goal.next_planned_action}
                </p>
              )}
              {goal.blockers.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {goal.blockers.map((b, i) => (
                    <li key={i} className="text-xs text-status-warning">• {b}</li>
                  ))}
                </ul>
              )}
              <div className="mt-4 flex gap-2 border-t border-surface-300 pt-3">
                {goal.status === 'ACTIVE' ? (
                  <button onClick={() => pause.mutate(goal.id)} className="btn-ghost flex-1">
                    Pause
                  </button>
                ) : (
                  <button onClick={() => resume.mutate(goal.id)} className="btn-primary flex-1">
                    Resume
                  </button>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

// ==================================================================== Memory
export function Memory() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['memory'],
    queryFn: () => api.searchMemory('', 50),
  })
  const analyze = useMutation({ mutationFn: api.analyzeLearning })

  if (isLoading) return <Loading label="Loading memory" />
  if (error) return <ErrorState error={error} />

  const stats = (data?.stats ?? {}) as Record<string, unknown>
  const memories = data?.memories ?? []

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Memory & Learning</h1>
          <p className="mt-1 text-sm text-slate-400">
            Persistent knowledge, workflow patterns, and outcome analysis
          </p>
        </div>
        <button onClick={() => analyze.mutate()} disabled={analyze.isPending} className="btn-primary">
          <Brain className="h-4 w-4" />
          {analyze.isPending ? 'Analyzing…' : 'Run Learning Analysis'}
        </button>
      </header>

      <div className="grid grid-cols-3 gap-4">
        {[
          { label: 'Total Memories', value: String(stats.total ?? 0) },
          { label: 'Inferences', value: String(stats.inferences ?? 0) },
          { label: 'Expired', value: String(stats.expired ?? 0) },
        ].map((s) => (
          <Card key={s.label}>
            <p className="stat-label">{s.label}</p>
            <p className="stat-value mt-1">{s.value}</p>
          </Card>
        ))}
      </div>

      {analyze.data && (
        <Card title="Latest Learning Insights">
          <ul className="space-y-3">
            {(analyze.data.insights as Array<Record<string, unknown>>).map((insight, i) => (
              <li key={i} className="rounded-lg bg-surface-200 p-3">
                <p className="text-sm font-medium text-slate-200">{String(insight.topic)}</p>
                <p className="mt-1 text-xs text-slate-400">{String(insight.observation)}</p>
                {insight.recommendation ? (
                  <p className="mt-1 text-xs text-accent">→ {String(insight.recommendation)}</p>
                ) : null}
              </li>
            ))}
            {analyze.data.insight_count === 0 && (
              <p className="text-sm text-slate-500">
                Not enough execution history yet. Insights require a minimum sample size.
              </p>
            )}
          </ul>
        </Card>
      )}

      <Card title={`Memories (${memories.length})`}>
        {memories.length === 0 ? (
          <EmptyState title="No memories stored" icon={<Brain className="h-8 w-8" />} />
        ) : (
          <ul className="divide-y divide-surface-200">
            {memories.map((m) => (
              <li key={m.id} className="py-3">
                <div className="flex items-center justify-between gap-3">
                  <span className="badge bg-surface-300 text-slate-400">{m.category}</span>
                  <span className="text-xs text-slate-500">
                    confidence {(m.confidence * 100).toFixed(0)}%
                    {m.is_inference && ' · inference'}
                  </span>
                </div>
                <p className="mt-1.5 text-sm text-slate-300">{m.content}</p>
                <p className="mt-0.5 text-[11px] text-slate-600">source: {m.source}</p>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

// ==================================================================== Diagnostics
export function Diagnostics() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['diagnostics'],
    queryFn: api.diagnostics,
    refetchInterval: 20000,
  })
  if (isLoading) return <Loading label="Running diagnostics" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">System Diagnostics</h1>
          <p className="mt-1 text-sm text-slate-400">{data?.note}</p>
        </div>
        <span
          className={`badge ${
            data?.overall === 'HEALTHY' ? 'bg-green-900 text-green-300' : 'bg-amber-900 text-amber-300'
          }`}
        >
          {data?.overall}
        </span>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(data?.subsystems ?? {}).map(([name, info]) => {
          const healthy = info.status === 'ok' || info.status === 'HEALTHY'
          return (
            <Card key={name}>
              <div className="flex items-center justify-between">
                <h3 className="font-medium text-slate-200">{name}</h3>
                <span
                  className={`h-2.5 w-2.5 rounded-full ${
                    healthy ? 'bg-status-success' : 'bg-status-warning'
                  }`}
                />
              </div>
              <p className="mt-1 text-xs text-slate-500">{String(info.status)}</p>
              {info.last_checked_at && (
                <p className="mt-2 text-[11px] text-slate-600">
                  checked {new Date(info.last_checked_at).toLocaleString()}
                </p>
              )}
            </Card>
          )
        })}
      </div>
    </div>
  )
}

// ==================================================================== Placeholders
function Placeholder({
  title,
  description,
  icon,
  note,
}: {
  title: string
  description: string
  icon: React.ReactNode
  note?: string
}) {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-slate-100">{title}</h1>
        <p className="mt-1 text-sm text-slate-400">{description}</p>
      </header>
      <EmptyState title="Backend endpoint pending" description={note} icon={icon} />
    </div>
  )
}

export const Airdrops = () => (
  <Placeholder
    title="Airdrop Explorer"
    description="Discovered campaigns, eligibility, deadlines, and risk"
    icon={<Compass className="h-8 w-8" />}
    note="The campaigns listing endpoint is not yet exposed by the backend. The discovery and verification engines exist in backend/app/airdrops/."
  />
)

export const Transactions = () => (
  <Placeholder
    title="Transactions"
    description="On-chain activity, confirmations, and cost accounting"
    icon={<Activity className="h-8 w-8" />}
    note="The transactions listing endpoint is not yet exposed by the backend. The transaction engine and receipt tracker exist in backend/app/transactions/."
  />
)

export const Social = () => (
  <Placeholder
    title="Social Monitors"
    description="Discord and X announcement tracking"
    icon={<Radio className="h-8 w-8" />}
    note="Discord and X adapters are specified but not yet implemented; the Telegram integration is functional."
  />
)

export const Automation = () => (
  <Placeholder
    title="24/7 Automation"
    description="Schedules, recurring jobs, and monitoring intervals"
    icon={<Zap className="h-8 w-8" />}
    note="The scheduler and worker daemon are implemented. A schedule-management API is not yet exposed."
  />
)

export const Evidence = () => (
  <Placeholder
    title="Evidence & Changes"
    description="Source claims, verification history, and change records"
    icon={<LifeBuoy className="h-8 w-8" />}
    note="The change detector and source verifier exist in backend/app/airdrops/verifier.py; a query endpoint is not yet exposed."
  />
)

export function Improvements() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['improvements'],
    queryFn: api.listImprovements,
  })
  if (isLoading) return <Loading label="Loading proposals" />
  if (error) return <ErrorState error={error} />

  const proposals = data?.proposals ?? []
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-slate-100">Self-Improvement</h1>
        <p className="mt-1 text-sm text-slate-400">
          Proposed code changes awaiting controlled validation
        </p>
      </header>
      {proposals.length === 0 ? (
        <EmptyState
          title="No proposals"
          description="The self-coding engine is implemented; proposals are created by the agent when it identifies a concrete problem."
          icon={<GitBranch className="h-8 w-8" />}
        />
      ) : (
        <div className="space-y-3">
          {proposals.map((p) => (
            <Card key={p.id}>
              <div className="flex items-start justify-between gap-3">
                <h3 className="font-medium text-slate-100">{p.title}</h3>
                <span className="badge shrink-0 bg-surface-300 text-slate-400">{p.status}</span>
              </div>
              {p.security_review_required && (
                <p className="mt-2 text-xs text-status-warning">
                  Security review required before deployment.
                </p>
              )}
              {p.affected_files.length > 0 && (
                <ul className="mt-2 space-y-0.5">
                  {p.affected_files.map((f) => (
                    <li key={f} className="font-mono text-xs text-slate-500">{f}</li>
                  ))}
                </ul>
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
