/** Tool Registry: inspect every registered capability and its health. */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, Globe, Lock, Search, ShieldAlert, Wallet, Wrench } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { api } from '@/services/api'
import { Card, EmptyState, ErrorState, IconTile, Loading, PageHeader, Pill } from '@/components/ui'
import clsx from 'clsx'

const SIDE_EFFECT_TONE: Record<string, string> = {
  none: 'text-status-idle',
  external_read: 'text-status-info',
  web_interaction: 'text-status-warning',
  wallet_signing: 'text-status-error',
  transaction_submission: 'text-status-error',
  state_mutation: 'text-status-warning',
}

const SIDE_EFFECT_ICON: Record<string, { icon: LucideIcon; tone: 'cyan' | 'orange' | 'red' | 'purple' | 'slate' }> = {
  external_read: { icon: Search, tone: 'cyan' },
  web_interaction: { icon: Globe, tone: 'orange' },
  wallet_signing: { icon: Wallet, tone: 'red' },
  transaction_submission: { icon: Wallet, tone: 'red' },
  state_mutation: { icon: Wrench, tone: 'purple' },
}

export default function ToolsPage() {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['tools'],
    queryFn: api.listTools,
    refetchInterval: 15000,
  })

  if (isLoading) return <Loading label="Loading tool registry" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />

  const tools = data?.tools ?? []
  const needle = query.trim().toLowerCase()
  const shown = needle
    ? tools.filter((t) => t.name.toLowerCase().includes(needle) || t.description.toLowerCase().includes(needle))
    : tools

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title={
          <>
            <span className="lg:hidden">Tools</span>
            <span className="hidden lg:inline">Tool Registry</span>
          </>
        }
        subtitle={
          <>
            <span className="lg:hidden">Access available tools</span>
            <span className="hidden lg:inline">
              Every capability the agent may invoke. The LLM can only select tools that appear here
              and are authorized.
            </span>
          </>
        }
      />

      {/* Phone: searchable list */}
      <div className="space-y-3 lg:hidden">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-5 w-5 -translate-y-1/2 text-slate-500" />
          <input
            className="input pl-11"
            placeholder="Search tools…"
            aria-label="Search tools"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        {shown.length > 0 && (
          <ul className="overflow-hidden rounded-2xl border border-surface-300/60 bg-surface-100 shadow-card">
            {shown.map((tool) => {
              const visual = SIDE_EFFECT_ICON[tool.side_effect] ?? { icon: Wrench, tone: 'slate' as const }
              const Icon = visual.icon
              const expanded = open === tool.name
              return (
                <li key={tool.name} className="border-b border-surface-300/60 last:border-b-0">
                  <button
                    type="button"
                    onClick={() => setOpen(expanded ? null : tool.name)}
                    aria-expanded={expanded}
                    className="flex w-full items-center gap-3 px-3 py-3 text-left"
                  >
                    <IconTile tone={visual.tone}>
                      <Icon />
                    </IconTile>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[15px] font-semibold text-slate-100">{tool.name}</p>
                      <p className="truncate text-xs text-slate-500">{tool.description}</p>
                    </div>
                    <ChevronRight
                      className={clsx('h-5 w-5 shrink-0 text-slate-500 transition-transform', expanded && 'rotate-90')}
                    />
                  </button>

                  {expanded && (
                    <div className="space-y-2 px-3 pb-3 text-xs">
                      <p className="leading-relaxed text-slate-400">{tool.description}</p>
                      <div className="flex flex-wrap gap-1">
                        <Pill tone={tool.enabled ? 'green' : 'gray'}>{tool.enabled ? 'Enabled' : 'Disabled'}</Pill>
                        {tool.authorized === false && <Pill tone="amber">Unauthorized</Pill>}
                        <Pill tone="gray">v{tool.version}</Pill>
                      </div>
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
                        <p className="rounded bg-status-error/10 px-2 py-1 text-[11px] text-status-error">
                          {tool.disabled_reason}
                        </p>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
        {tools.length > 0 && shown.length === 0 && (
          <p className="py-6 text-center text-sm text-slate-500">No tools match “{query}”.</p>
        )}
      </div>

      {/* Desktop: full registry cards */}
      <div className="hidden gap-4 sm:grid-cols-2 lg:grid lg:grid-cols-3">
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
              <p className="rounded bg-status-error/10 px-2 py-1 text-[11px] text-status-error">
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
