/** Security & Settings: emergency stop, limits, LLM configuration, integrations. */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronRight, KeyRound, LogOut, PlayCircle, Plug, Power } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { api, UNAUTHORIZED_EVENT } from '@/services/api'
import { Alert, Card, IconTile, Loading, PageHeader } from '@/components/ui'
import { NAV_SECTIONS } from '@/layouts/navigation'

/** Pages that are not in the bottom tab bar; the phone's "More" list links to them. */
const MORE_ROUTES = new Set([
  '/goals',
  '/tools',
  '/transactions',
  '/worker',
  '/diagnostics',
  '/memory',
  '/evidence',
  '/social',
  '/automation',
  '/browser',
  '/telegram',
  '/improvements',
])

function SettingsRow({
  icon: Icon,
  tone,
  title,
  subtitle,
  to,
  onClick,
}: {
  icon: LucideIcon
  tone: 'blue' | 'green' | 'purple' | 'orange' | 'red' | 'cyan' | 'slate'
  title: string
  subtitle?: string
  to?: string
  onClick?: () => void
}) {
  const content = (
    <>
      <IconTile tone={tone}>
        <Icon />
      </IconTile>
      <span className="min-w-0 flex-1 text-left">
        <span className="block truncate text-[15px] font-semibold text-slate-100">{title}</span>
        {subtitle && <span className="block truncate text-xs text-slate-500">{subtitle}</span>}
      </span>
      <ChevronRight className="h-5 w-5 shrink-0 text-slate-500" />
    </>
  )
  const cls = 'flex w-full items-center gap-3 border-b border-surface-300/60 px-3 py-3 last:border-b-0'
  return to ? (
    <Link to={to} className={cls}>
      {content}
    </Link>
  ) : (
    <button type="button" onClick={onClick} className={cls}>
      {content}
    </button>
  )
}

function jumpTo(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

export default function SecuritySettings() {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')

  const { data: stop, isLoading, error: stopError, refetch: refetchStop } = useQuery({
    queryKey: ['emergency-stop'],
    queryFn: api.stopStatus,
    refetchInterval: 5000,
  })

  const { data: agent, error: agentError } = useQuery({
    queryKey: ['agent-info'],
    queryFn: api.agentInfo,
  })

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

  // The kill switch must never be reported as inactive on the strength of a
  // failed request. `stop?.x ?? false` turned a 403/500/network drop into a
  // green "INACTIVE" badge, so an operator who could not reach the status
  // endpoint was told the platform was safe. Unknown is its own state.
  const stopKnown = Boolean(stop) && !stopError
  const stopped = stopKnown ? Boolean(stop?.emergency_stop_enabled) : null

  const moreItems = NAV_SECTIONS.flatMap((section) => section.items).filter((item) => MORE_ROUTES.has(item.to))

  return (
    <div className="flex flex-col gap-4 pt-1 lg:gap-6 lg:pt-0">
      <PageHeader
        title={
          <>
            <span className="lg:hidden">Settings</span>
            <span className="hidden lg:inline">Security & Settings</span>
          </>
        }
        subtitle={
          <>
            <span className="lg:hidden">Manage your application</span>
            <span className="hidden lg:inline">
              Deterministic controls that the agent cannot modify or disable
            </span>
          </>
        }
      />

      {/* Phone: shortcuts into this page, then every page that is not in the tab bar */}
      <div className="space-y-4 lg:hidden">
        <div className="overflow-hidden rounded-2xl border border-surface-300/60 bg-surface-100 shadow-card">
          <SettingsRow
            icon={KeyRound}
            tone="slate"
            title="Fixed LLM configuration"
            subtitle="Model and service settings (read-only)"
            onClick={() => jumpTo('llm')}
          />
          <SettingsRow
            icon={Plug}
            tone="blue"
            title="Integrations"
            subtitle="Telegram, browser, RPC and scheduler"
            onClick={() => jumpTo('integrations')}
          />
        </div>

        <div>
          <p className="mb-2 px-1 text-[11px] font-semibold uppercase tracking-widest text-slate-500">More</p>
          <div className="overflow-hidden rounded-2xl border border-surface-300/60 bg-surface-100 shadow-card">
            {moreItems.map(({ to, label, icon }) => (
              <SettingsRow key={to} icon={icon} tone="blue" title={label} to={to} />
            ))}
            <SettingsRow
              icon={LogOut}
              tone="slate"
              title="Log out"
              onClick={async () => {
                await api.logout()
                window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
              }}
            />
          </div>
        </div>
      </div>

      {/* Emergency Stop (last on phones, as in the design) */}
      <div id="emergency-stop" className="scroll-mt-4 max-lg:order-last">
      <Card
        title="Emergency Stop"
        className={
          stopped === null
            ? 'border-status-error/50'
            : stopped
              ? 'border-status-error/50'
              : 'max-lg:border-status-error/30 max-lg:bg-status-error/5'
        }
        action={
          stopped === null ? (
            <span className="badge bg-slate-700 text-slate-200">UNKNOWN</span>
          ) : (
            <span className={`badge ${stopped ? 'bg-red-700 text-white' : 'bg-green-900 text-green-300'}`}>
              {stopped ? 'ACTIVE' : 'INACTIVE'}
            </span>
          )
        }
      >
        {stopped === null ? (
          <Alert kind="error" title="Stop status could not be read">
            AirdropOS could not determine whether the emergency stop is engaged. Until this
            resolves, treat the platform as if it might be stopped: do not rely on this page to
            tell you the stop is off.
            {(engage.error || release.error) && (
              <span className="mt-2 block text-xs opacity-80">
                Last action failed: {String((engage.error ?? release.error))}
              </span>
            )}
            <button type="button" className="btn btn-ghost mt-3" onClick={() => void refetchStop()}>
              Retry
            </button>
          </Alert>
        ) : (
          <Alert kind={stopped ? 'error' : 'success'} title={stop?.disclaimer}>
            When engaged, the platform prevents new task execution, new wallet signing, and new
            transaction submission. Records and checkpoints are preserved.
          </Alert>
        )}

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

        {/* A refused or failed engage/release used to be invisible: both mutations
            had onSuccess only, so the button simply stopped spinning and the
            operator could not tell whether the kill switch had engaged. */}
        {(engage.error || release.error) && stopped !== null && (
          <p className="mt-2 text-xs text-status-error">
            {release.error
              ? 'Resume was refused: '
              : 'Engage failed: '}
            {String(release.error ?? engage.error)}
          </p>
        )}
      </Card>
      </div>

      {/* Fixed model */}
      {agentError && (
        <div id="llm" className="scroll-mt-4">
          <Card title="Fixed LLM Configuration">
            <Alert kind="error" title="LLM configuration could not be read">
              AirdropOS could not load the fixed-model settings. {String(agentError)}
            </Alert>
          </Card>
        </div>
      )}
      {agent && (
        <div id="llm" className="scroll-mt-4">
        <Card title="Fixed LLM Configuration">
          <div className="grid gap-3 sm:grid-cols-2">
            {[
              { label: 'Endpoint', value: agent.base_url, mono: true },
              { label: 'Model ID (requested)', value: agent.model_id, mono: true },
              { label: 'API key', value: agent.api_key_masked || '(not configured)', mono: true },
              { label: 'Policy', value: agent.policy },
              { label: 'Temperature', value: String(agent.temperature) },
              { label: 'Max tokens', value: agent.max_tokens == null ? 'No app limit' : String(agent.max_tokens) },
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
          {/* Routing is invisible unless it is shown. AirdropOS requests one
              model; it cannot control what the endpoint actually runs. */}
          {agent.served_models_observed && agent.served_models_observed.length > 0 && (
            <div className="mt-3 rounded-lg bg-surface-200 px-3 py-2">
              <p className="text-[10px] uppercase tracking-wide text-slate-500">
                Models the provider said it served
              </p>
              <p className="mt-1 font-mono text-sm text-slate-200">
                {agent.served_models_observed.join(', ')}
              </p>
              {agent.served_models_observed.some((m) => m !== agent.model_id) && (
                <p className="mt-2 text-xs text-status-error">
                  The endpoint served a model other than the one requested. This is provider-side
                  routing and AirdropOS cannot prevent it; set LLM_MODEL_ID to an id the endpoint
                  actually serves.
                </p>
              )}
            </div>
          )}
          {agent.context_budget && (
            <p className="mt-3 text-xs text-slate-500">
              Input context budget: {agent.context_budget.context_window} tokens, of which{' '}
              {agent.context_budget.usable_input} are usable for input after reserving{' '}
              {agent.context_budget.reserved_output} for output and{' '}
              {agent.context_budget.safety_margin} of safety margin. Compaction begins above{' '}
              {agent.context_budget.trigger} and aims for {agent.context_budget.target}.
            </p>
          )}
        </Card>
        </div>
      )}

      {/* Integrations */}
      <div id="integrations" className="scroll-mt-4">
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
    </div>
  )
}
