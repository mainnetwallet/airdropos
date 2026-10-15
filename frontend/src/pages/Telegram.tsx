/** Telegram Integration: readiness of the Telegram user client, from the worker's real status. */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CheckCircle2, ChevronRight, Info, Plus, Send, Star, Trash2, UserRound, XCircle } from 'lucide-react'
import { api } from '@/services/api'
import AddAccountModal from '@/components/telegram/AddAccountModal'
import RemoveAccountModal from '@/components/telegram/RemoveAccountModal'
import type { TelegramAccount } from '@/types'
import { Card, ErrorState, IconTile, Loading, PageHeader, Pill } from '@/components/ui'

export default function Telegram() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['worker-status-mini'],
    queryFn: api.workerStatus,
    refetchInterval: 5000,
  })

  const accountsQuery = useQuery({
    queryKey: ['telegram-accounts'],
    queryFn: api.telegramAccounts,
    refetchInterval: 15000,
  })
  const accounts = accountsQuery.data?.accounts ?? []
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<TelegramAccount | null>(null)
  const makeDefault = useMutation({
    mutationFn: (label: string) => api.telegramSetDefault(label),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['telegram-accounts'] }),
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

      {/* Accounts */}
      <Card
        title={`Telegram accounts (${accounts.length})`}
        action={
          <button type="button" onClick={() => setAdding(true)} className="btn btn-primary !px-3 !py-1.5 text-xs">
            <Plus className="h-4 w-4" /> Add account
          </button>
        }
      >
        {accountsQuery.isLoading ? (
          <p className="py-4 text-center text-sm text-slate-500">Loading accounts…</p>
        ) : accountsQuery.error ? (
          <p className="py-4 text-center text-sm text-status-warning">Could not load Telegram accounts.</p>
        ) : accounts.length === 0 ? (
          <p className="py-4 text-center text-sm text-slate-500">No account is logged in yet. Tap “Add account”.</p>
        ) : (
          <ul className="divide-y divide-surface-300/70">
            {accounts.map((a) => (
              <li key={a.label} className="flex items-center gap-3 py-3 first:pt-0 last:pb-0">
                <IconTile tone={a.logged_in ? 'blue' : 'slate'} className="h-10 w-10">
                  <UserRound />
                </IconTile>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-slate-100">
                    {a.profile_name ?? 'Unnamed profile'}
                  </p>
                  <p className="truncate text-xs text-slate-500">
                    {a.username ? `@${a.username}` : 'no username'}
                    {a.user_id ? ` · id ${a.user_id}` : ''}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <Pill tone="blue">{a.label}</Pill>
                  <div className="flex gap-1">
                    {a.is_default && <Pill tone="green">Default</Pill>}
                    {!a.logged_in && <Pill tone="red">Logged out</Pill>}
                  </div>
                </div>
                <div className="flex shrink-0 gap-1">
                  {!a.is_default && !a.legacy && (
                    <button
                      type="button"
                      title="Make default"
                      aria-label={`Make ${a.label} the default account`}
                      disabled={makeDefault.isPending}
                      onClick={() => makeDefault.mutate(a.label)}
                      className="rounded-lg p-2 text-slate-400 hover:bg-surface-200 hover:text-slate-100"
                    >
                      <Star className="h-4 w-4" />
                    </button>
                  )}
                  <button
                    type="button"
                    title="Remove"
                    aria-label={`Remove ${a.label}`}
                    onClick={() => setRemoving(a)}
                    className="rounded-lg p-2 text-slate-400 hover:bg-surface-200 hover:text-status-error"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {makeDefault.error && <p className="mt-3 text-xs text-status-error">{(makeDefault.error as Error).message}</p>}
        <p className="mt-3 text-xs text-slate-500">
          The agent uses the default account unless a task names a label.
        </p>
      </Card>
      {adding && <AddAccountModal hasAccounts={accounts.length > 0} onClose={() => setAdding(false)} />}
      {removing && <RemoveAccountModal account={removing} onClose={() => setRemoving(null)} />}

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
