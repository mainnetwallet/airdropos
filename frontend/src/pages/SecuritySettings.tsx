/** Security & Settings: emergency stop, limits, LLM configuration, integrations. */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, PlayCircle, Power } from 'lucide-react'
import { api } from '@/services/api'
import { Alert, Card, Loading } from '@/components/ui'

export default function SecuritySettings() {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')

  const { data: stop, isLoading } = useQuery({
    queryKey: ['emergency-stop'],
    queryFn: api.stopStatus,
    refetchInterval: 5000,
  })

  const { data: agent } = useQuery({ queryKey: ['agent-info'], queryFn: api.agentInfo })

  const engage = useMutation({
    mutationFn: () => api.engageStop(reason || 'Manual stop from dashboard'),
    onSuccess: () => {
      setReason('')
      queryClient.invalidateQueries({ queryKey: ['emergency-stop'] })
      queryClient.invalidateQueries({ queryKey: ['overview'] })
    },
  })

  const release = useMutation({
    mutationFn: () => api.releaseStop(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['emergency-stop'] }),
  })

  if (isLoading) return <Loading label="Loading security settings" />

  const stopped = stop?.emergency_stop_enabled ?? false

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-slate-100">Security & Settings</h1>
        <p className="mt-1 text-sm text-slate-400">
          Deterministic controls that the agent cannot modify or disable
        </p>
      </header>

      {/* Emergency Stop */}
      <Card
        title="Emergency Stop"
        className={stopped ? 'border-status-error/50' : ''}
        action={
          <span className={`badge ${stopped ? 'bg-red-700 text-white' : 'bg-green-900 text-green-300'}`}>
            {stopped ? 'ACTIVE' : 'INACTIVE'}
          </span>
        }
      >
        <Alert kind={stopped ? 'error' : 'success'} title={stop?.disclaimer}>
          When engaged, the platform prevents new task execution, new wallet signing, and new
          transaction submission. Records and checkpoints are preserved.
        </Alert>

        {stopped && stop?.activated_at && (
          <div className="mt-4 space-y-1 rounded-lg bg-surface-200 p-3 text-xs">
            <p>
              <span className="text-slate-500">Activated:</span>{' '}
              <span className="text-slate-300">{stop.activated_at}</span>
            </p>
            <p>
              <span className="text-slate-500">By:</span>{' '}
              <span className="text-slate-300">{stop.activated_by}</span>
            </p>
            <p>
              <span className="text-slate-500">Reason:</span>{' '}
              <span className="text-slate-300">{stop.reason}</span>
            </p>
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-2">
          {!stopped ? (
            <>
              <input
                className="input flex-1 min-w-64"
                placeholder="Reason for stopping (required, minimum 3 characters)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button
                onClick={() => engage.mutate()}
                disabled={engage.isPending || reason.trim().length < 3}
                className="btn-danger"
              >
                <Power className="h-4 w-4" />
                Engage Stop
              </button>
            </>
          ) : (
            <button onClick={() => release.mutate()} disabled={release.isPending} className="btn-primary">
              <PlayCircle className="h-4 w-4" />
              Resume Operation
            </button>
          )}
        </div>
      </Card>

      {/* Fixed model */}
      {agent && (
        <Card title="Fixed LLM Configuration">
          <div className="grid gap-3 sm:grid-cols-2">
            {[
              { label: 'Endpoint', value: agent.base_url, mono: true },
              { label: 'Model ID', value: agent.model_id, mono: true },
              { label: 'API key', value: agent.api_key_masked || '(not configured)', mono: true },
              { label: 'Policy', value: agent.policy },
              { label: 'Temperature', value: String(agent.temperature) },
              { label: 'Max tokens', value: String(agent.max_tokens) },
              { label: 'Timeout', value: `${agent.timeout_seconds}s` },
              { label: 'Retries', value: String(agent.max_retries) },
            ].map((field) => (
              <div key={field.label} className="rounded-lg bg-surface-200 px-3 py-2">
                <p className="text-[10px] uppercase tracking-wide text-slate-500">{field.label}</p>
                <p className={`truncate text-sm text-slate-200 ${field.mono ? 'font-mono' : ''}`}>
                  {field.value}
                </p>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-slate-500">
            The stored API key is never returned to the browser after saving. The masked value above
            is display-only.
          </p>
        </Card>
      )}

      {/* Integrations */}
      <Card title="Integrations">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[
            { name: 'Telegram', note: 'Bot token + authorized user IDs configured in .env' },
            { name: 'Discord', note: 'Bot token with Message Content Intent' },
            { name: 'X / Twitter', note: 'Official API v2 credentials' },
            { name: 'RPC Providers', note: 'Per-network endpoints for EVM chains' },
            { name: 'Browser', note: 'Playwright Chromium, session isolation enabled' },
            { name: 'Scheduler', note: 'Database-persisted recurring jobs' },
          ].map((item) => (
            <div key={item.name} className="rounded-lg border border-surface-300 p-3">
              <div className="flex items-center gap-2">
                <AlertTriangle className="h-4 w-4 text-slate-500" />
                <p className="text-sm font-medium text-slate-200">{item.name}</p>
              </div>
              <p className="mt-1 text-xs text-slate-500">{item.note}</p>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Nonessential integrations stay disabled when credentials are absent. Security controls are
          never silently weakened to make an integration work.
        </p>
      </Card>
    </div>
  )
}
