/** Remaining dashboard pages, grouped to keep the router simple. */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  Brain,
  Compass,
  GitBranch,
  LifeBuoy,
  Plus,
  Radio,
  Search,
  Target,
  Zap,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '@/services/api'
import {
  Card,
  Chips,
  EmptyState,
  ErrorState,
  IconTile,
  Loading,
  PageHeader,
  ProgressBar,
  SegmentedTabs,
} from '@/components/ui'

// ==================================================================== Goals
type GoalsTab = 'goals' | 'strategies' | 'progress'

export function Goals() {
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ['goals'], queryFn: api.listGoals })
  const { data: overview } = useQuery({ queryKey: ['overview'], queryFn: api.overview, refetchInterval: 10000 })
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [tab, setTab] = useState<GoalsTab>('goals')
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
  const active = goals.filter((g) => g.status === 'ACTIVE').length
  const average = goals.length ? goals.reduce((sum, g) => sum + g.progress, 0) / goals.length : 0

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title={
          <>
            <span className="lg:hidden">Goals & Planning</span>
            <span className="hidden lg:inline">Goals & Strategy</span>
          </>
        }
        subtitle={
          <>
            <span className="lg:hidden">Set goals and track progress</span>
            <span className="hidden lg:inline">Persistent long-term objectives</span>
          </>
        }
        action={
          // Goals are defined by the agent, so this opens Chat with a draft (nothing is sent).
          <button
            type="button"
            aria-label="Define a new goal with the agent"
            onClick={() => navigate('/agent', { state: { prompt: 'Define a new long-term goal: ' } })}
            className="flex h-11 w-11 items-center justify-center rounded-full border border-accent/40 bg-accent/10 text-accent lg:hidden"
          >
            <Plus className="h-5 w-5" />
          </button>
        }
      />

      <SegmentedTabs
        value={tab}
        onChange={setTab}
        items={[
          { id: 'goals', label: 'Goals' },
          { id: 'strategies', label: 'Strategies' },
          { id: 'progress', label: 'Progress' },
        ]}
      />

      {/* Goals */}
      <div className={clsx(tab !== 'goals' && 'hidden lg:block')}>
        {goals.length === 0 ? (
          <EmptyState
            title="No goals defined"
            description="Ask the agent to define a long-term goal, e.g. 'Continuously discover and complete legitimate airdrops.'"
            icon={<Target className="h-8 w-8" />}
          />
        ) : (
          <div className="grid gap-3 lg:grid-cols-2 lg:gap-4">
            {goals.map((goal) => (
              <Card key={goal.id}>
                <div className="flex items-start gap-3">
                  <IconTile tone={goal.status === 'ACTIVE' ? 'orange' : 'slate'} className="lg:hidden">
                    <Target />
                  </IconTile>
                  <div className="flex min-w-0 flex-1 items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-mono text-xs text-slate-500">{goal.goal_code}</p>
                      <h3 className="mt-0.5 font-medium text-slate-100">{goal.title}</h3>
                    </div>
                    <span
                      className={`badge shrink-0 ${goal.status === 'ACTIVE' ? 'bg-green-900 text-green-300' : 'bg-slate-800 text-slate-400'}`}
                    >
                      {goal.status}
                    </span>
                  </div>
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
                    <button onClick={() => pause.mutate(goal.id)} className="btn-ghost flex-1 justify-center">
                      Pause
                    </button>
                  ) : (
                    <button onClick={() => resume.mutate(goal.id)} className="btn-primary flex-1 justify-center">
                      Resume
                    </button>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>

      {/* Strategies (phone tab): the backend does not expose these yet */}
      {tab === 'strategies' && (
        <div className="lg:hidden">
          <EmptyState
            title="No strategy data"
            description="Strategies are not exposed by the backend yet. Goals carry their next planned action."
            icon={<Compass className="h-8 w-8" />}
          />
        </div>
      )}

      {/* Progress (phone tab): computed from real goals and task counts */}
      {tab === 'progress' && (
        <div className="space-y-3 lg:hidden">
          <Card>
            <p className="stat-label">Average goal progress</p>
            <p className="stat-value mt-1">{Math.round(average * 100)}%</p>
            <ProgressBar value={average} className="mt-3" />
            <p className="mt-2 text-xs text-slate-500">
              {goals.length} {goals.length === 1 ? 'goal' : 'goals'}, {active} active
            </p>
          </Card>
          {overview && (
            <div className="grid grid-cols-3 gap-2 text-center">
              {[
                { label: 'Active tasks', value: overview.active_tasks },
                { label: 'Completed', value: overview.completed_tasks },
                { label: 'Failed', value: overview.failed_tasks },
              ].map((item) => (
                <Card key={item.label} className="px-2">
                  <p className="text-xl font-bold text-slate-100">{item.value}</p>
                  <p className="text-xs text-slate-500">{item.label}</p>
                </Card>
              ))}
            </div>
          )}
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
          <h1 className="text-[26px] font-bold leading-tight text-slate-100 lg:text-2xl">Memory & Learning</h1>
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
          <h1 className="text-[26px] font-bold leading-tight text-slate-100 lg:text-2xl">System Diagnostics</h1>
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
        <h1 className="text-[26px] font-bold leading-tight text-slate-100 lg:text-2xl">{title}</h1>
        <p className="mt-1 text-sm text-slate-400">{description}</p>
      </header>
      <EmptyState title="Backend endpoint pending" description={note} icon={icon} />
    </div>
  )
}

/**
 * Phone layout shared by the two list screens whose backend endpoints do not exist yet.
 * The search field and filters are shown as in the design but disabled, and the list is an
 * honest empty state. Nothing here is sample data.
 */
function PendingList({
  title,
  mobileTitle,
  subtitle,
  mobileSubtitle,
  searchPlaceholder,
  filters,
  knownTotal,
  icon,
  note,
  askLabel,
  askPrompt,
}: {
  title: string
  mobileTitle: string
  subtitle: string
  mobileSubtitle: string
  searchPlaceholder: string
  filters: string[]
  /** Real total, when some other endpoint reports one. */
  knownTotal?: number
  icon: React.ReactNode
  note: string
  askLabel: string
  askPrompt: string
}) {
  const navigate = useNavigate()
  const [first, ...rest] = filters

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title={
          <>
            <span className="lg:hidden">{mobileTitle}</span>
            <span className="hidden lg:inline">{title}</span>
          </>
        }
        subtitle={
          <>
            <span className="lg:hidden">{mobileSubtitle}</span>
            <span className="hidden lg:inline">{subtitle}</span>
          </>
        }
      />

      <div className="relative lg:hidden">
        <Search className="pointer-events-none absolute left-3.5 top-1/2 h-5 w-5 -translate-y-1/2 text-slate-500" />
        <input
          className="input pl-11"
          placeholder={searchPlaceholder}
          aria-label={searchPlaceholder}
          disabled
        />
      </div>
      <Chips
        className="lg:hidden"
        value={first}
        onChange={() => undefined}
        items={[
          { id: first, label: first, count: knownTotal, disabled: true },
          ...rest.map((label) => ({ id: label, label, disabled: true })),
        ]}
      />

      <EmptyState title="Backend endpoint pending" description={note} icon={icon} />

      <button
        type="button"
        onClick={() => navigate('/agent', { state: { prompt: askPrompt } })}
        className="btn-primary mx-auto justify-center rounded-xl lg:hidden"
      >
        {askLabel}
      </button>
    </div>
  )
}

export const Airdrops = () => {
  const { data } = useQuery({ queryKey: ['overview'], queryFn: api.overview, refetchInterval: 10000 })
  return (
    <PendingList
      title="Airdrop Explorer"
      mobileTitle="Airdrops"
      subtitle="Discovered campaigns, eligibility, deadlines, and risk"
      mobileSubtitle="Discover and track real opportunities"
      searchPlaceholder="Search airdrops, chains, tags…"
      filters={['All', 'Active', 'Potential', 'Completed']}
      knownTotal={data?.campaigns}
      icon={<Compass className="h-8 w-8" />}
      note="The campaigns listing endpoint is not yet exposed by the backend. The discovery and verification engines exist in backend/app/airdrops/."
      askLabel="Ask the agent to find airdrops"
      askPrompt="Find new legitimate airdrops and summarize the eligibility requirements."
    />
  )
}

export const Transactions = () => (
  <PendingList
    title="Transactions"
    mobileTitle="Transactions"
    subtitle="On-chain activity, confirmations, and cost accounting"
    mobileSubtitle="Track all blockchain transactions"
    searchPlaceholder="Search transactions…"
    filters={['All', 'Pending', 'Completed', 'Failed']}
    icon={<Activity className="h-8 w-8" />}
    note="The transactions listing endpoint is not yet exposed by the backend. The transaction engine and receipt tracker exist in backend/app/transactions/."
    askLabel="Ask the agent about recent transactions"
    askPrompt="Summarize my recent transactions and their status."
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
        <h1 className="text-[26px] font-bold leading-tight text-slate-100 lg:text-2xl">Self-Improvement</h1>
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
