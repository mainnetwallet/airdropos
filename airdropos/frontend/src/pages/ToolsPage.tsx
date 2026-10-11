/** Tool Registry: inspect every registered capability and its health. */

import { useQuery } from '@tanstack/react-query'
import { Lock, ShieldAlert, Wrench } from 'lucide-react'
import { api } from '@/services/api'
import { Card, EmptyState, ErrorState, Loading } from '@/components/ui'
import clsx from 'clsx'

const SIDE_EFFECT_TONE: Record<string, string> = {
  none: 'text-status-idle',
  external_read: 'text-status-info',
  web_interaction: 'text-status-warning',
  wallet_signing: 'text-status-error',
  transaction_submission: 'text-status-error',
  state_mutation: 'text-status-warning',
}

export default function ToolsPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['tools'],
    queryFn: api.listTools,
    refetchInterval: 15000,
  })

  if (isLoading) return <Loading label="Loading tool registry" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />

  const tools = data?.tools ?? []

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-slate-100">Tool Registry</h1>
        <p className="mt-1 text-sm text-slate-400">
          Every capability the agent may invoke. The LLM can only select tools that appear here
          and are authorized.
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {tools.map((tool) => (
          <Card key={tool.name} className="flex flex-col gap-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 className="truncate font-mono text-sm text-accent">{tool.name}</h3>
                <p className="text-xs text-slate-500">v{tool.version}</p>
              </div>
              <div className="flex shrink-0 gap-1">
                {tool.enabled ? (
                  <span className="badge bg-green-900 text-green-300">enabled</span>
                ) : (
                  <span className="badge bg-slate-800 text-slate-400">disabled</span>
                )}
                {tool.authorized === false && (
                  <span className="badge bg-amber-900 text-amber-300">unauthorized</span>
                )}
              </div>
            </div>

            <p className="flex-1 text-xs leading-relaxed text-slate-400">{tool.description}</p>

            <div className="space-y-1.5 border-t border-surface-300 pt-2 text-xs">
              <div className="flex justify-between">
                <span className="text-slate-500">Side effect</span>
                <span className={clsx('font-medium', SIDE_EFFECT_TONE[tool.side_effect])}>
                  {tool.side_effect.replace(/_/g, ' ')}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Calls</span>
                <span className="text-slate-300">{tool.metrics.total_calls}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Success rate</span>
                <span className="text-slate-300">
                  {tool.metrics.success_rate !== null
                    ? `${Math.round(tool.metrics.success_rate * 100)}%`
                    : '—'}
                </span>
              </div>
            </div>

            {tool.required_permissions.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {tool.required_permissions.map((p) => (
                  <span
                    key={p}
                    className="inline-flex items-center gap-1 rounded bg-surface-300 px-1.5 py-0.5 text-[10px] text-slate-400"
                  >
                    <Lock className="h-2.5 w-2.5" />
                    {p}
                  </span>
                ))}
              </div>
            )}

            {tool.disabled_reason && (
              <p className="rounded bg-red-900/40 px-2 py-1 text-[11px] text-red-300">
                {tool.disabled_reason}
              </p>
            )}
          </Card>
        ))}
      </div>

      {tools.length === 0 && (
        <EmptyState
          title="No tools registered"
          description="Tools register at backend startup."
          icon={<Wrench className="h-8 w-8" />}
        />
      )}

      <Card title="Security Note">
        <div className="flex gap-3 text-sm text-slate-400">
          <ShieldAlert className="h-5 w-5 shrink-0 text-status-warning" />
          <p>
            There is no unrestricted shell, SQL, filesystem, or raw network access exposed to the
            LLM. Tools marked <span className="text-status-error">wallet signing</span> or{' '}
            <span className="text-status-error">transaction submission</span> are not granted by
            default and additionally pass deterministic validation in the vault and transaction
            engine before any key material is released.
          </p>
        </div>
      </Card>
    </div>
  )
}
