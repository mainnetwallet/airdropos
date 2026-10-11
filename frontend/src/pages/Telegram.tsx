/** Telegram Integration: readiness of the Telegram user client, from the worker's real status. */

import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CheckCircle2, ChevronRight, Info, Send, XCircle } from 'lucide-react'
import { api } from '@/services/api'
import { Card, ErrorState, IconTile, Loading, PageHeader, Pill } from '@/components/ui'

export default function Telegram() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['worker-status-mini'],
    queryFn: api.workerStatus,
    refetchInterval: 5000,
  })

  if (isLoading) return <Loading label="Checking Telegram" />
  if (error || !data) return <ErrorState error={error} onRetry={() => refetch()} />

  const { telegram, worker } = data
  const tools = worker.telegram_tools ?? []
  const checks = [
    { ok: telegram.enabled, label: 'Telegram user client enabled' },
    { ok: telegram.api_id_set && telegram.api_hash_set, label: 'API ID and API hash set' },
    { ok: telegram.telethon_installed, label: 'Telethon installed' },
    { ok: telegram.session_file_exists, label: 'Logged in (session file exists)' },
  ]
  const passed = checks.filter((c) => c.ok).length

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title="Telegram Integration"
        subtitle="Status of the Telegram user client used by the worker"
      />

      {/* Connection */}
      <Card className="flex items-center gap-3">
        <IconTile tone={telegram.ready ? 'blue' : 'slate'} className="h-12 w-12">
          <Send />
        </IconTile>
        <div className="min-w-0 flex-1">
          <p className={telegram.ready ? 'font-semibold text-status-success' : 'font-semibold text-status-warning'}>
            {telegram.ready ? 'Connected' : 'Not connected'}
          </p>
          <p className="text-xs text-slate-500">
            {telegram.ready
              ? worker.tool_count !== undefined
                ? 'Ready — tools are registered in the worker'
                : 'Ready'
              : (telegram.hint ?? 'Setup is incomplete')}
          </p>
        </div>
        <Link
          to="/worker"
          className="shrink-0 rounded-xl bg-accent px-3.5 py-2 text-xs font-semibold text-white"
        >
          Worker
        </Link>
      </Card>

      {/* Summary */}
      <Card className="grid grid-cols-3 divide-x divide-surface-300 p-0 max-lg:p-0">
        {[
          { label: 'Tools', value: String(tools.length) },
          { label: 'Setup checks', value: `${passed}/${checks.length}` },
          { label: 'Worker', value: data.alive ? 'Running' : 'Offline' },
        ].map((item) => (
          <div key={item.label} className="px-2 py-4 text-center">
            <p className="text-xl font-bold text-slate-100">{item.value}</p>
            <p className="text-xs text-slate-500">{item.label}</p>
          </div>
        ))}
      </Card>

      {/* Setup checks */}
      <Card title="Setup">
        <ul className="divide-y divide-surface-300/70">
          {checks.map((check) => (
            <li key={check.label} className="flex items-center gap-3 py-3 first:pt-0 last:pb-0">
              {check.ok ? (
                <CheckCircle2 className="h-5 w-5 shrink-0 text-status-success" />
              ) : (
                <XCircle className="h-5 w-5 shrink-0 text-status-error" />
              )}
              <span className="flex-1 text-sm text-slate-200">{check.label}</span>
              <Pill tone={check.ok ? 'green' : 'red'}>{check.ok ? 'OK' : 'Missing'}</Pill>
            </li>
          ))}
        </ul>
        {telegram.hint && <p className="mt-3 text-xs text-status-warning">{telegram.hint}</p>}
      </Card>

      {/* Tools */}
      <Card title="Telegram tools">
        {tools.length === 0 ? (
          <p className="py-4 text-center text-sm text-slate-500">
            No Telegram tools are registered in the worker.
          </p>
        ) : (
          <ul className="divide-y divide-surface-300/70">
            {tools.map((name) => (
              <li key={name} className="flex items-center justify-between py-2.5 first:pt-0 last:pb-0">
                <span className="truncate font-mono text-sm text-slate-200">{name}</span>
                <Link to="/tools" aria-label={`Open ${name} in the tool registry`}>
                  <ChevronRight className="h-4 w-4 text-slate-500" />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <p className="flex items-start gap-2 px-1 text-xs text-slate-500">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        Message history and command statistics are not exposed by the backend yet, so they are not shown here.
      </p>
    </div>
  )
}
