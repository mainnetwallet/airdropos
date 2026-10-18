/**
 * X.com: one page for both halves of the integration.
 *
 * Browser half  - the existing persistent Chrome profiles, so the operator's own
 *                 X login is reused. Login is done BY THE OPERATOR; this page
 *                 only reports what the page actually shows.
 * API half      - several named X developer configurations, each with its own
 *                 encrypted app credentials and authorized account.
 *
 * Two properties this page is built around:
 *
 * 1. It never shows a secret, and never asks for one it does not need. The
 *    backend is write-only for credentials; the response only carries masks.
 * 2. It never reports an unverified state as a working one. A configuration
 *    that has not been tested says UNVERIFIED; a browser profile whose worker is
 *    unreachable says unknown, not "signed out".
 */

import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  CircleHelp,
  ExternalLink,
  FileUp,
  Globe,
  KeyRound,
  Loader2,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  Star,
  Trash2,
  Upload,
} from 'lucide-react'
import clsx from 'clsx'
import {
  Alert,
  Card,
  Chips,
  EmptyState,
  ErrorState,
  Loading,
  PageHeader,
  Pill,
  SegmentedTabs,
} from '@/components/ui'
import Modal from '@/components/live/Modal'
import { api, liveApi } from '@/services/api'
import type { BrowserProfileInfo } from '@/types/live'
import type {
  XActivityRecord,
  XApiConfiguration,
  XApiConfigurationInput,
  XExecutionPreference,
  XImportPreview,
} from '@/types/x'

type Tab = 'overview' | 'browser' | 'api' | 'settings' | 'activity'

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'browser', label: 'Browser' },
  { id: 'api', label: 'API' },
  { id: 'settings', label: 'Settings' },
  { id: 'activity', label: 'Activity' },
]

const EMPTY_FORM: XApiConfigurationInput = {
  name: '',
  description: '',
  client_id: '',
  client_secret: '',
  access_token: '',
  refresh_token: '',
  username: '',
  enabled: true,
  default: false,
}

const PREFERENCE_OPTIONS: Array<{ id: XExecutionPreference; label: string }> = [
  { id: 'auto', label: 'Auto' },
  { id: 'api_preferred', label: 'API preferred' },
  { id: 'browser_preferred', label: 'Browser preferred' },
]

// ---------------------------------------------------------------- helpers
function statusTone(status: string): 'green' | 'blue' | 'amber' | 'red' | 'gray' {
  if (status === 'OK') return 'green'
  if (status === 'UNVERIFIED') return 'blue'
  if (status === 'RATE_LIMITED') return 'amber'
  if (status === 'AUTH_REQUIRED' || status === 'INVALID') return 'red'
  return 'gray'
}

function when(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return 'unknown'
  return date.toLocaleString()
}

function errorText(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  try {
    return JSON.stringify(error)
  } catch {
    return 'Unknown error'
  }
}

/** Short banner for a failed mutation. Never renders a secret. */
function MutationError({ error }: { error: unknown }) {
  if (!error) return null
  return (
    <Alert kind="error" title="That did not work">
      {errorText(error)}
    </Alert>
  )
}

// ==================================================================== Page
export default function XIntegration() {
  const [tab, setTab] = useState<Tab>('overview')
  const queryClient = useQueryClient()
  const refreshAll = () => queryClient.invalidateQueries({ queryKey: ['x'] })

  const overview = useQuery({
    queryKey: ['x', 'overview'],
    queryFn: api.xOverview,
    refetchInterval: 15_000,
  })

  const activity = useQuery({
    queryKey: ['x', 'activity'],
    queryFn: () => api.xActivity({ limit: 100 }),
    refetchInterval: 30_000,
  })

  if (overview.isLoading) return <Loading label="Loading X.com integration" />
  if (overview.error) return <ErrorState error={overview.error} onRetry={() => overview.refetch()} />

  const data = overview.data
  const counts = data?.counts ?? {
    recent_success: 0,
    recent_failed: 0,
    recent_uncertain: 0,
    recent_needs_user: 0,
  }

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title={
          <>
            <span className="lg:hidden">X.com</span>
            <span className="hidden lg:inline">X.com Integration</span>
          </>
        }
        subtitle={
          <>
            <span className="lg:hidden">Browser + official API</span>
            <span className="hidden lg:inline">
              Persistent browser profiles and the official X API, with one agent capability over both
            </span>
          </>
        }
        action={
          <button type="button" onClick={refreshAll} className="btn-ghost" aria-label="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
        }
      />

      {!data?.vault.ready && (
        <Alert kind="error" title="The credential vault is not ready">
          {data?.vault.detail ||
            'X API credentials cannot be encrypted on this deployment, so the API half is unavailable.'}
        </Alert>
      )}
      {data && !data.api_enabled && (
        <Alert kind="warning" title="The X API is switched off">
          <code>X_API_ENABLED=false</code>. Browser mode still works; API-backed operations report the
          API as unavailable rather than silently doing something else.
        </Alert>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          { label: 'Authorized', value: data?.authorized_count ?? 0 },
          { label: 'Verified', value: data?.verified_count ?? 0 },
          { label: 'Uncertain writes', value: counts.recent_uncertain, warn: counts.recent_uncertain > 0 },
          { label: 'Needs a human', value: counts.recent_needs_user },
        ].map((s) => (
          <Card key={s.label} className="px-3 py-2.5">
            <p className={clsx('text-xl font-bold', s.warn ? 'text-status-warning' : 'text-slate-100')}>
              {s.value}
            </p>
            <p className="text-[11px] text-slate-500">{s.label}</p>
          </Card>
        ))}
      </div>

      <SegmentedTabs value={tab} onChange={setTab} items={TABS} />

      {tab === 'overview' && <OverviewTab data={data} />}
      {tab === 'browser' && <BrowserTab />}
      {tab === 'api' && <ApiTab />}
      {tab === 'settings' && <SettingsTab data={data} />}
      {tab === 'activity' && (
        <ActivityTab
          rows={activity.data?.activity ?? []}
          loading={activity.isLoading}
          error={activity.error}
          note={activity.data?.note}
          onRetry={() => activity.refetch()}
        />
      )}
    </div>
  )
}

// ==================================================================== Overview
function OverviewTab({ data }: { data: Awaited<ReturnType<typeof api.xOverview>> | undefined }) {
  if (!data) return null
  const browser = data.browser ?? {}
  const recent = data.recent_activity.slice(0, 8)

  return (
    <div className="space-y-4">
      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="Browser mode">
          <Row label="Profile" value={data.browser_profile_id || 'none selected'} mono />
          <p className="mt-1 text-xs text-slate-500">{data.browser_profile_reason}</p>
          <div className="mt-3 space-y-1 border-t border-surface-300 pt-3 text-xs">
            <Row label="Browser" value={browser.running ? `running (${browser.mode ?? 'managed'})` : 'not running'} />
            <Row label="X login" value={browser.login ?? 'unknown'} />
            {browser.active_url && <Row label="Current page" value={browser.active_url} mono />}
            {browser.last_error && (
              <p className="text-status-error">Last error: {String(browser.last_error)}</p>
            )}
            {!browser.running && (
              <p className="text-slate-500">
                Sign in to X yourself in the Live Broadcast view. AirdropOS never enters your
                password, and never bypasses a CAPTCHA, MFA prompt or login challenge.
              </p>
            )}
          </div>
        </Card>

        <Card title="API mode">
          <Row label="API" value={data.api_enabled ? 'enabled' : 'disabled'} />
          <Row label="Vault" value={`${data.vault.algorithm} · ${data.vault.key_source}`} />
          <Row label="Configurations" value={`${data.enabled_count} enabled of ${data.configuration_count}`} />
          <Row label="Authorized" value={`${data.authorized_count} with a user token`} />
          <div className="mt-3 border-t border-surface-300 pt-3">
            <p className="text-xs font-medium text-slate-300">Scopes requested at authorization</p>
            <div className="mt-1 flex flex-wrap gap-1">
              {data.required_scopes.map((s) => (
                <span key={s} className="badge bg-surface-300 font-mono text-slate-400">
                  {s}
                </span>
              ))}
            </div>
          </div>
        </Card>
      </div>

      <Alert kind="info" title="What the API can and cannot do">
        {data.note}
      </Alert>

      <Card title="How the agent routes X work">
        <p className="-mt-2 mb-3 text-xs text-slate-500">
          Currently: {data.preference.replace('_', ' ')}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1.5 pr-3 font-medium">Operation</th>
                <th className="py-1.5 pr-3 font-medium">API</th>
                <th className="py-1.5 pr-3 font-medium">Browser</th>
                <th className="py-1.5 font-medium">Note</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-300">
              {data.catalog.operations.map((op) => (
                <tr key={op.name}>
                  <td className="py-1.5 pr-3 text-slate-200">
                    {op.label}
                    {op.write && <span className="ml-1 text-[10px] text-status-warning">write</span>}
                  </td>
                  <td className="py-1.5 pr-3">
                    {op.supported_by.includes('api') ? (
                      <span className="text-emerald-500">yes</span>
                    ) : (
                      <span className="text-slate-600">no</span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3">
                    {op.supported_by.includes('browser') ? (
                      <span className="text-emerald-500">yes</span>
                    ) : (
                      <span className="text-slate-600">no</span>
                    )}
                  </td>
                  <td className="py-1.5 text-slate-500">{op.api_caveat || op.browser_note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Recent activity">
        {recent.length === 0 ? (
          <EmptyState
            title="No X actions yet"
            description="Ask the agent to read a profile, publish a post, like something, or follow an account."
            icon={<Globe className="h-8 w-8" />}
          />
        ) : (
          <ul className="divide-y divide-surface-200">
            {recent.map((row) => (
              <ActivityRow key={row.id} row={row} compact />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-xs">
      <span className="text-slate-500">{label}</span>
      <span className={clsx('truncate text-slate-200', mono && 'font-mono')}>{value}</span>
    </div>
  )
}

// ==================================================================== Browser
function BrowserTab() {
  const queryClient = useQueryClient()
  const [profileId, setProfileId] = useState<string>('')

  const profiles = useQuery({ queryKey: ['browser', 'profiles'], queryFn: liveApi.browserProfiles })
  const chosen = useQuery({
    queryKey: ['x', 'browser-profile'],
    queryFn: api.xBrowserProfile,
  })
  const status = useQuery({
    queryKey: ['x', 'browser-status', profileId],
    queryFn: () => api.xBrowserStatus(profileId || undefined),
    enabled: true,
    refetchInterval: 10_000,
  })

  const choose = useMutation({
    mutationFn: (id: string | null) => api.chooseXBrowserProfile(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['x'] })
      queryClient.invalidateQueries({ queryKey: ['browser'] })
    },
  })

  const open = useMutation({
    mutationFn: (id: string) => liveApi.openBrowserProfile(id, 'https://x.com/home'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['x'] })
      queryClient.invalidateQueries({ queryKey: ['browser'] })
    },
  })

  if (profiles.isLoading) return <Loading label="Loading browser profiles" />
  if (profiles.error) return <ErrorState error={profiles.error} onRetry={() => profiles.refetch()} />

  const all: BrowserProfileInfo[] = profiles.data?.profiles ?? []
  const selectedId = profileId || chosen.data?.browser_profile_id || ''
  const selected = all.find((p) => p.id === selectedId)
  const live = status.data

  return (
    <div className="space-y-4">
      <Card title="Browser profile used for X">
        <p className="-mt-2 mb-3 text-xs text-slate-500">
          A managed, persistent Chrome profile. Its cookies and logins survive restarts, so the X
          session you sign into is reused.
        </p>
        <label className="block text-xs font-medium text-slate-400">
          Profile
          <select
            className="input mt-1 w-full"
            value={selectedId}
            onChange={(e) => setProfileId(e.target.value)}
          >
            <option value="">Operator's default agent profile</option>
            {all.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.id}){p.is_default_agent ? ' — current default' : ''}
              </option>
            ))}
          </select>
        </label>
        <p className="mt-1 text-xs text-slate-500">{chosen.data?.reason}</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-primary"
            disabled={choose.isPending || !selectedId}
            onClick={() => choose.mutate(selectedId)}
          >
            {choose.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Pin this profile for X
          </button>
          <button
            type="button"
            className="btn-ghost"
            disabled={choose.isPending || !chosen.data?.pinned_profile_id}
            onClick={() => choose.mutate(null)}
          >
            Use the default agent profile
          </button>
          {selected && (
            <button
              type="button"
              className="btn-ghost"
              disabled={open.isPending}
              onClick={() => open.mutate(selected.id)}
            >
              {open.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              Open X.com
            </button>
          )}
        </div>
        <div className="mt-3 space-y-2">
          <MutationError error={choose.error} />
          <MutationError error={open.error} />
        </div>
      </Card>

      <Card title="Live state">
        {status.isLoading ? (
          <Loading label="Reading the browser session" />
        ) : !live?.ok ? (
          <Alert kind="warning" title="The X browser session could not be read">
            {live?.detail || 'The browser worker may be down. Nothing is assumed about your X login.'}
          </Alert>
        ) : (
          <div className="space-y-1 text-xs">
            <Row label="Session" value={live.session_key ?? ''} mono />
            <Row label="Browser" value={live.running ? `running (${live.mode})` : 'not running'} />
            <Row label="X login" value={live.login ?? 'unknown'} />
            {live.active_url && <Row label="Current page" value={live.active_url} mono />}
            {live.limitations && live.limitations.length > 0 && (
              <ul className="mt-2 space-y-0.5 text-slate-500">
                {live.limitations.map((l) => (
                  <li key={l}>• {l}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Card>

      {all.length === 0 && (
        <EmptyState
          title="No browser profiles yet"
          description="Create one on the Browser Profiles page. AirdropOS launches a managed, persistent Chrome profile you can sign into."
          icon={<Globe className="h-8 w-8" />}
        />
      )}
    </div>
  )
}

// ==================================================================== API configs
function ApiTab() {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<XApiConfiguration | 'new' | null>(null)
  const [importing, setImporting] = useState(false)
  const [authorizing, setAuthorizing] = useState<XApiConfiguration | null>(null)

  const configs = useQuery({ queryKey: ['x', 'configurations'], queryFn: api.xConfigurations })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['x'] })

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.setXConfigurationEnabled(id, enabled),
    onSuccess: invalidate,
  })
  const makeDefault = useMutation({ mutationFn: (id: string) => api.setDefaultXConfiguration(id), onSuccess: invalidate })
  const remove = useMutation({ mutationFn: (id: string) => api.deleteXConfiguration(id), onSuccess: invalidate })

  if (configs.isLoading) return <Loading label="Loading API configurations" />
  if (configs.error) return <ErrorState error={configs.error} onRetry={() => configs.refetch()} />

  const rows = configs.data?.configurations ?? []

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-primary" onClick={() => setEditing('new')}>
          <Plus className="h-4 w-4" />
          Add configuration
        </button>
        <button type="button" className="btn-ghost" onClick={() => setImporting(true)}>
          <Upload className="h-4 w-4" />
          Bulk import
        </button>
      </div>

      <div className="space-y-2">
        <MutationError error={toggle.error} />
        <MutationError error={makeDefault.error} />
        <MutationError error={remove.error} />
      </div>

      {rows.length === 0 ? (
        <EmptyState
          title="No API configurations"
          description="Add an X developer app, or use Browser mode. Without a configuration, the agent uses your signed-in browser profile."
          icon={<KeyRound className="h-8 w-8" />}
        />
      ) : (
        <div className="space-y-3">
          {rows.map((row) => (
            <ConfigurationCard
              key={row.id}
              row={row}
              onEdit={() => setEditing(row)}
              onAuthorize={() => setAuthorizing(row)}
              onToggle={(enabled) => toggle.mutate({ id: row.id, enabled })}
              onDefault={() => makeDefault.mutate(row.id)}
              onDelete={() => {
                if (window.confirm(`Delete "${row.name}" and its stored secrets?\n\nActivity history is kept.`)) {
                  remove.mutate(row.id)
                }
              }}
            />
          ))}
        </div>
      )}

      {editing && (
        <ConfigurationForm
          existing={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            invalidate()
          }}
        />
      )}
      {importing && <ImportDialog onClose={() => { setImporting(false); invalidate() }} />}
      {authorizing && (
        <AuthorizeDialog config={authorizing} onClose={() => { setAuthorizing(null); invalidate() }} />
      )}
    </div>
  )
}

function ConfigurationCard({
  row,
  onEdit,
  onAuthorize,
  onToggle,
  onDefault,
  onDelete,
}: {
  row: XApiConfiguration
  onEdit: () => void
  onAuthorize: () => void
  onToggle: (enabled: boolean) => void
  onDefault: () => void
  onDelete: () => void
}) {
  const queryClient = useQueryClient()
  const [result, setResult] = useState<string>('')

  const test = useMutation({
    mutationFn: () => api.testXConfiguration(row.id),
    onSuccess: (data) => {
      setResult(
        data.ok
          ? `Authorized as @${data.user?.username ?? '?'} (id ${data.user?.id ?? '?'}).`
          : `${data.status}: ${data.error ?? 'the connection test failed'}`,
      )
      queryClient.invalidateQueries({ queryKey: ['x'] })
    },
  })

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-medium text-slate-100">{row.name}</h3>
            {row.is_default && <Pill tone="blue">default</Pill>}
            {!row.is_enabled && <Pill tone="gray">disabled</Pill>}
            <Pill tone={statusTone(row.status)}>{row.status}</Pill>
          </div>
          {row.description && <p className="mt-1 text-xs text-slate-500">{row.description}</p>}
        </div>
      </div>

      <dl className="mt-3 grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
        <Row label="App key" value={row.client_id_preview || '—'} mono />
        <Row label="Account" value={row.x_username ? `@${row.x_username}` : 'not authorized'} />
        <Row label="App secret" value={row.has_client_secret ? row.client_secret_mask : 'not set'} />
        <Row label="User token" value={row.has_access_token ? row.access_token_mask : 'not set'} />
        <Row label="Refresh token" value={row.has_refresh_token ? 'stored' : 'none'} />
        <Row label="Last checked" value={when(row.last_checked_at)} />
        <Row label="Last used" value={when(row.last_used_at)} />
        <Row label="Rate limit left" value={row.rate_limit_remaining === null ? 'unknown' : String(row.rate_limit_remaining)} />
      </dl>

      {row.scopes.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {row.scopes.map((s) => (
            <span key={s} className="badge bg-surface-300 font-mono text-slate-400">
              {s}
            </span>
          ))}
        </div>
      )}

      {row.status_detail && (
        <p className="mt-2 text-xs text-slate-400">{row.status_detail}</p>
      )}
      {!row.can_write && row.is_enabled && (
        <p className="mt-2 text-xs text-status-warning">
          No authorized X account yet, so this configuration can only be used once you complete the
          authorization step.
        </p>
      )}
      {result && <p className="mt-2 text-xs text-slate-300">{result}</p>}
      <MutationError error={test.error} />

      <div className="mt-3 flex flex-wrap gap-2 border-t border-surface-300 pt-3">
        <button
          type="button"
          className="btn-ghost"
          onClick={onAuthorize}
          disabled={!row.has_client_secret && !row.client_id_preview}
        >
          <ShieldCheck className="h-4 w-4" />
          Authorize
        </button>
        <button
          type="button"
          className="btn-ghost"
          onClick={() => test.mutate()}
          disabled={test.isPending || !row.is_enabled}
        >
          {test.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
          Test connection
        </button>
        <button type="button" className="btn-ghost" onClick={onEdit}>
          <Pencil className="h-4 w-4" />
          Edit
        </button>
        <button type="button" className="btn-ghost" onClick={() => onToggle(!row.is_enabled)}>
          {row.is_enabled ? 'Disable' : 'Enable'}
        </button>
        {!row.is_default && (
          <button type="button" className="btn-ghost" onClick={onDefault}>
            <Star className="h-4 w-4" />
            Make default
          </button>
        )}
        <button type="button" className="btn-ghost text-status-error" onClick={onDelete} aria-label={`Delete ${row.name}`}>
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
    </Card>
  )
}

// ---------------------------------------------------------------- Form
function ConfigurationForm({
  existing,
  onClose,
  onSaved,
}: {
  existing: XApiConfiguration | null
  onClose: () => void
  onSaved: () => void
}) {
  const [v, setV] = useState<XApiConfigurationInput>({
    ...EMPTY_FORM,
    name: existing?.name ?? '',
    description: existing?.description ?? '',
    client_id: '',
    username: existing?.x_username ?? '',
    enabled: existing?.is_enabled ?? true,
    default: existing?.is_default ?? false,
  })
  const set = <K extends keyof XApiConfigurationInput>(k: K, value: XApiConfigurationInput[K]) =>
    setV((p) => ({ ...p, [k]: value }))

  const save = useMutation({
    mutationFn: async () => {
      const body = { ...v, name: v.name.trim(), client_id: v.client_id.trim() }
      if (existing) {
        // Blank secret fields mean "leave the stored value alone". The backend
        // enforces that, so the form does not have to guess.
        await api.updateXConfiguration(existing.id, body)
      } else {
        await api.createXConfiguration(body)
      }
    },
    onSuccess: onSaved,
  })

  return (
    <Modal title={existing ? `Edit ${existing.name}` : 'Add X API configuration'} onClose={onClose}>
      <form
        className="max-h-[70vh] space-y-3 overflow-y-auto"
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        <label className="block text-xs font-medium text-slate-400">
          Configuration name
          <input
            className="input mt-1 w-full"
            required
            maxLength={120}
            value={v.name}
            onChange={(e) => set('name', e.target.value)}
            placeholder="e.g. Main X account"
          />
        </label>

        <label className="block text-xs font-medium text-slate-400">
          Description
          <input
            className="input mt-1 w-full"
            maxLength={400}
            value={v.description}
            onChange={(e) => set('description', e.target.value)}
          />
        </label>

        <fieldset className="rounded-lg border border-surface-200 p-3">
          <legend className="px-1 text-xs font-semibold text-slate-300">App credentials (developer console)</legend>
          <label className="mt-2 block text-xs font-medium text-slate-400">
            Client ID / consumer key
            <input
              className="input mt-1 w-full font-mono"
              maxLength={255}
              value={v.client_id}
              onChange={(e) => set('client_id', e.target.value)}
              required={!existing}
            />
          </label>
          <label className="mt-2 block text-xs font-medium text-slate-400">
            Client secret
            <input
              className="input mt-1 w-full font-mono"
              type="password"
              autoComplete="off"
              maxLength={512}
              value={v.client_secret}
              onChange={(e) => set('client_secret', e.target.value)}
              placeholder={existing ? '•••• leave blank to keep the stored secret' : ''}
            />
          </label>
          <p className="mt-1 text-[11px] text-slate-500">
            Stored encrypted with AES-256-GCM. It is never returned by the API and never logged. Do
            not paste it into source control.
          </p>
        </fieldset>

        <fieldset className="rounded-lg border border-surface-200 p-3">
          <legend className="px-1 text-xs font-semibold text-slate-300">Account authorization</legend>
          <label className="mt-2 block text-xs font-medium text-slate-400">
            Access token (optional — the Authorize button is preferred)
            <input
              className="input mt-1 w-full font-mono"
              type="password"
              autoComplete="off"
              maxLength={2048}
              value={v.access_token}
              onChange={(e) => set('access_token', e.target.value)}
              placeholder={existing ? '•••• leave blank to keep the stored token' : ''}
            />
          </label>
          <label className="mt-2 block text-xs font-medium text-slate-400">
            Refresh token (optional — needed to renew without re-consent)
            <input
              className="input mt-1 w-full font-mono"
              type="password"
              autoComplete="off"
              maxLength={2048}
              value={v.refresh_token}
              onChange={(e) => set('refresh_token', e.target.value)}
            />
          </label>
          <label className="mt-2 block text-xs font-medium text-slate-400">
            Expected @handle (optional, verified by Test connection)
            <input
              className="input mt-1 w-full"
              maxLength={64}
              value={v.username}
              onChange={(e) => set('username', e.target.value)}
              placeholder="@handle"
            />
          </label>
        </fieldset>

        <div className="flex flex-wrap gap-4 text-xs text-slate-300">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={v.enabled} onChange={(e) => set('enabled', e.target.checked)} />
            Enabled
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={v.default} onChange={(e) => set('default', e.target.checked)} />
            Preferred configuration
          </label>
        </div>

        <MutationError error={save.error} />

        <div className="flex gap-2 pt-1">
          <button type="submit" className="btn-primary flex-1 justify-center" disabled={save.isPending}>
            {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Save configuration
          </button>
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ---------------------------------------------------------------- Bulk import
function ImportDialog({ onClose }: { onClose: () => void }) {
  const [content, setContent] = useState('')
  const [filename, setFilename] = useState('')
  const [preview, setPreview] = useState<XImportPreview | null>(null)
  const [selected, setSelected] = useState<number[]>([])
  const fileRef = useRef<HTMLInputElement>(null)

  const runPreview = useMutation({
    mutationFn: () => api.previewXImport(content, filename),
    onSuccess: (data) => {
      setPreview(data)
      setSelected(data.rows.map((r) => r.row))
    },
  })

  const commit = useMutation({
    mutationFn: () => api.importXConfigurations(content, filename, selected),
    onSuccess: onClose,
  })

  const readFile = async (file: File) => {
    setFilename(file.name)
    setContent(await file.text())
  }

  return (
    <Modal title="Bulk import API configurations" onClose={onClose}>
      <div className="space-y-3">
        <Alert kind="info" title="Nothing is saved until you import">
          Import is validated first: bad fields, duplicate names and conflicting defaults are
          reported per row, and you choose which valid rows to write.
        </Alert>

        <input
          ref={fileRef}
          type="file"
          accept=".json,.csv,application/json,text/csv"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) void readFile(file)
          }}
        />
        <button type="button" className="btn-ghost" onClick={() => fileRef.current?.click()}>
          <FileUp className="h-4 w-4" />
          Choose a .json or .csv file
        </button>
        {filename && <p className="text-xs text-slate-500">{filename}</p>}

        <label className="block text-xs font-medium text-slate-400">
          …or paste JSON / CSV
          <textarea
            className="input mt-1 h-40 w-full font-mono text-[11px]"
            value={content}
            onChange={(e) => {
              setContent(e.target.value)
              setFilename('')
            }}
            placeholder='[{"name":"Main","client_id":"...","client_secret":"..."}]'
          />
        </label>

        <button
          type="button"
          className="btn-primary w-full justify-center"
          onClick={() => runPreview.mutate()}
          disabled={!content.trim() || runPreview.isPending}
        >
          {runPreview.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
          Validate
        </button>
        <MutationError error={runPreview.error} />

        {preview && (
          <div className="space-y-3">
            <p className="text-xs text-slate-400">
              {preview.valid_count} valid row(s), {preview.error_count} problem(s).
            </p>

            {preview.errors.length > 0 && (
              <div className="rounded-lg border border-status-error/40 p-2">
                <p className="text-xs font-semibold text-status-error">Rejected rows</p>
                <ul className="mt-1 space-y-1 text-[11px] text-slate-300">
                  {preview.errors.map((e, i) => (
                    <li key={i}>
                      row {e.row || '—'} {e.name && `(${e.name})`}: <span className="text-slate-500">{e.field}</span> —{' '}
                      {e.message}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {preview.warnings.length > 0 && (
              <div className="rounded-lg border border-status-warning/40 p-2">
                <p className="text-xs font-semibold text-status-warning">Warnings</p>
                <ul className="mt-1 space-y-1 text-[11px] text-slate-300">
                  {preview.warnings.map((w, i) => (
                    <li key={i}>
                      row {w.row} ({w.name}): {w.message}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {preview.rows.length > 0 && (
              <div className="rounded-lg border border-surface-200 p-2">
                <p className="text-xs font-semibold text-slate-300">Accepted rows</p>
                <ul className="mt-1 space-y-1">
                  {preview.rows.map((r) => (
                    <li key={r.row} className="flex items-start gap-2 text-[11px]">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={selected.includes(r.row)}
                        onChange={(e) =>
                          setSelected((p) =>
                            e.target.checked ? [...p, r.row] : p.filter((x) => x !== r.row),
                          )
                        }
                      />
                      <span className="text-slate-300">
                        <span className="font-medium">{r.name}</span>{' '}
                        <span className="font-mono text-slate-500">{r.client_id_preview}</span>
                        {r.username && <span className="ml-1 text-slate-500">@{r.username.replace(/^@/, '')}</span>}
                        <span className="ml-1 text-slate-600">
                          {[r.has_client_secret && 'app secret', r.has_access_token && 'access token', r.has_refresh_token && 'refresh token']
                            .filter(Boolean)
                            .join(', ') || 'no secrets'}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <details className="text-xs">
              <summary className="cursor-pointer text-slate-400">Supported schema</summary>
              <pre className="mt-1 overflow-x-auto rounded bg-surface-200 p-2 font-mono text-[10px] text-slate-400">
                {JSON.stringify(preview.schema, null, 2)}
              </pre>
            </details>

            <MutationError error={commit.error} />
            <button
              type="button"
              className="btn-primary w-full justify-center"
              onClick={() => commit.mutate()}
              disabled={commit.isPending || selected.length === 0}
            >
              {commit.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Import {selected.length} configuration(s)
            </button>
          </div>
        )}
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- OAuth
function AuthorizeDialog({ config, onClose }: { config: XApiConfiguration; onClose: () => void }) {
  const [code, setCode] = useState('')
  const [state, setState] = useState('')
  const [message, setMessage] = useState('')
  const [start, setStart] = useState<Awaited<ReturnType<typeof api.startXAuthorization>> | null>(null)

  const begin = useMutation({
    mutationFn: () => api.startXAuthorization(config.id),
    onSuccess: (data) => {
      setStart(data)
      setState(data.state)
    },
  })
  const finish = useMutation({
    mutationFn: () => api.completeXAuthorization(config.id, code.trim(), state),
    onSuccess: (data) => {
      setMessage(data.note)
      setCode('')
    },
  })

  return (
    <Modal title={`Authorize ${config.name}`} onClose={onClose}>
      <div className="space-y-3">
        <Alert kind="info" title="You authorize X yourself">
          AirdropOS never sees your X password. X shows you the exact scopes and you approve them.
        </Alert>

        {!start && (
          <button
            type="button"
            className="btn-primary w-full justify-center"
            onClick={() => begin.mutate()}
            disabled={begin.isPending}
          >
            {begin.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Start authorization
          </button>
        )}
        <MutationError error={begin.error} />
        <MutationError error={finish.error} />

        {start && (
          <>
            <a
              className="btn-ghost w-full justify-center"
              href={start.authorization_url}
              target="_blank"
              rel="noreferrer"
            >
              <ExternalLink className="h-4 w-4" />
              Open the X authorization page
            </a>
            <p className="text-[11px] text-slate-500">
              Redirect URI (must match your app exactly): <span className="font-mono">{start.redirect_uri || 'NOT CONFIGURED'}</span>
            </p>
            <ul className="space-y-1 text-[11px] text-slate-500">
              {start.scopes.map((s) => (
                <li key={s} className="font-mono">
                  {s}
                </li>
              ))}
            </ul>
            <label className="block text-xs font-medium text-slate-400">
              Paste the `code` from the URL X redirected you to
              <input
                className="input mt-1 w-full font-mono"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="code=…"
              />
            </label>
            <button
              type="button"
              className="btn-primary w-full justify-center"
              onClick={() => finish.mutate()}
              disabled={finish.isPending || !code.trim()}
            >
              {finish.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Exchange the code
            </button>
            {message && <Alert kind="success">{message}</Alert>}
          </>
        )}
      </div>
    </Modal>
  )
}

// ==================================================================== Settings
function SettingsTab({ data }: { data: Awaited<ReturnType<typeof api.xOverview>> | undefined }) {
  const queryClient = useQueryClient()
  const preference = useQuery({ queryKey: ['x', 'preference'], queryFn: api.xPreference })

  const setPreference = useMutation({
    mutationFn: (value: XExecutionPreference) => api.setXPreference(value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['x'] }),
  })

  const [windowHours, setWindowHours] = useState(
    String(preference.data?.duplicate_window_hours ?? 6),
  )
  const setWindow = useMutation({
    mutationFn: (hours: number) => api.setXDuplicateWindow(hours),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['x'] }),
  })

  const current = (preference.data?.preference ?? data?.preference ?? 'auto') as XExecutionPreference
  const explanation = preference.data?.explanation ?? {}

  return (
    <div className="space-y-4">
      <Card title="Execution preference">
        <p className="-mt-2 mb-3 text-xs text-slate-500">
          How the agent chooses between the official API and your signed-in browser.
        </p>
        <Chips
          value={current}
          onChange={(v) => setPreference.mutate(v)}
          items={PREFERENCE_OPTIONS.map((o) => ({ id: o.id, label: o.label }))}
        />
        {setPreference.isPending && (
          <p className="mt-2 flex items-center gap-2 text-xs text-slate-400">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Saving…
          </p>
        )}
        <MutationError error={setPreference.error} />
        <div className="mt-3 space-y-2 border-t border-surface-300 pt-3">
          {PREFERENCE_OPTIONS.map((o) => (
            <p key={o.id} className={clsx('text-xs', current === o.id ? 'text-slate-200' : 'text-slate-500')}>
              <span className="font-medium">{o.label}: </span>
              {explanation[o.id] ?? ''}
            </p>
          ))}
        </div>
        <p className="mt-3 text-[11px] text-slate-500">
          Any single task can still name a method. An explicit request is honoured when that method
          supports the operation and is authorized; otherwise the agent reports why it cannot.
        </p>
      </Card>

      <Card title="Duplicate prevention window">
        <p className="-mt-2 mb-3 text-xs text-slate-500">
          An identical completed X write (same account, same text, same target) is refused as a
          duplicate for this long, so a retry after a lost response cannot post twice.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input w-24"
            type="number"
            min={0}
            max={720}
            step={1}
            value={windowHours}
            onChange={(e) => setWindowHours(e.target.value)}
            aria-label="Duplicate window in hours"
          />
          <span className="text-xs text-slate-500">hours</span>
          <button
            type="button"
            className="btn-ghost"
            onClick={() => setWindow.mutate(Number(windowHours) || 0)}
            disabled={setWindow.isPending}
          >
            {setWindow.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Save
          </button>
        </div>
        <MutationError error={setWindow.error} />
        <p className="mt-3 text-[11px] text-slate-500">
          Set it to <strong>0</strong> to allow publishing the same text twice on purpose. A write
          whose outcome was <em>unknown</em> (a timeout or a lost response) is never repeated at
          any window — verify it with <code>x_verify_action</code> first.
        </p>
      </Card>

      <Card title="Scales and limitations">
        <ul className="space-y-2 text-xs text-slate-400">
          <li>
            <AlertTriangle className="mr-1 inline h-3.5 w-3.5 text-status-warning" />
            X API v2 is <strong>pay-per-usage credits</strong>, not a subscription: reads are
            billed per resource returned and writes per request. Requests are blocked when the
            credit balance is exhausted, and a <code>403</code> means the app or account is not
            permitted — it is reported, never routed around.
          </li>
          <li>
            The browser half is not used to route around an X refusal. A rate limit or a permission
            denial is respected and reported, never circumvented.
          </li>
          <li>
            Credentials are encrypted with {data?.vault.algorithm ?? 'AES-256-GCM'} using{' '}
            {data?.vault.key_source ?? 'the configured key'}, masked in this UI, and excluded from
            logs, task history and every API response.
          </li>
        </ul>
      </Card>
    </div>
  )
}

// ==================================================================== Activity
function ActivityTab({
  rows,
  loading,
  error,
  note,
  onRetry,
}: {
  rows: XActivityRecord[]
  loading: boolean
  error: unknown
  note?: string
  onRetry: () => void
}) {
  const [method, setMethod] = useState<'all' | 'api' | 'browser'>('all')
  const filtered = method === 'all' ? rows : rows.filter((r) => r.method === method)

  if (loading) return <Loading label="Loading activity" />
  if (error) return <ErrorState error={error} onRetry={onRetry} />

  return (
    <div className="space-y-3">
      <Chips
        value={method}
        onChange={setMethod}
        items={[
          { id: 'all' as const, label: 'All', count: rows.length },
          { id: 'api' as const, label: 'API', count: rows.filter((r) => r.method === 'api').length },
          { id: 'browser' as const, label: 'Browser', count: rows.filter((r) => r.method === 'browser').length },
        ]}
      />

      {note && (
        <Alert kind="info">
          <span className="flex items-start gap-2">
            <CircleHelp className="mt-0.5 h-4 w-4 shrink-0" />
            {note}
          </span>
        </Alert>
      )}

      {filtered.length === 0 ? (
        <EmptyState
          title="No activity yet"
          description="Every X action the agent takes is recorded here, with the account, the method and the outcome."
          icon={<Globe className="h-8 w-8" />}
        />
      ) : (
        <ul className="space-y-2">
          {filtered.map((row) => (
            <ActivityRow key={row.id} row={row} />
          ))}
        </ul>
      )}
    </div>
  )
}

function ActivityRow({ row, compact }: { row: XActivityRecord; compact?: boolean }) {
  const tone =
    row.status === 'SUCCESS'
      ? 'green'
      : row.outcome_uncertain
        ? 'amber'
        : row.status === 'NEEDS_USER_ACTION'
          ? 'amber'
          : row.status === 'SKIPPED_DUPLICATE'
            ? 'blue'
            : 'red'

  return (
    <li className="rounded-lg border border-surface-200 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={tone}>{row.outcome_uncertain ? 'UNCERTAIN' : row.status}</Pill>
        <Pill tone={row.method === 'api' ? 'blue' : 'gray'}>{row.method}</Pill>
        <span className="text-xs font-medium text-slate-200">{row.operation}</span>
        <span className="truncate font-mono text-[11px] text-slate-500">{row.target}</span>
      </div>
      <p className="mt-1 text-[11px] text-slate-500">
        {row.config_name || row.browser_profile_id || 'unknown account'} · {when(row.created_at)}
        {row.task_id && <> · task <span className="font-mono">{row.task_id}</span></>}
        {row.duration_ms !== null && <> · {row.duration_ms} ms</>}
      </p>
      {row.error && <p className="mt-1 text-xs text-status-error">{row.error}</p>}
      {!compact && row.result_summary && Object.keys(row.result_summary).length > 0 && (
        <pre className="mt-1 overflow-x-auto rounded bg-surface-200 p-1.5 font-mono text-[10px] text-slate-400">
          {JSON.stringify(row.result_summary, null, 2)}
        </pre>
      )}
    </li>
  )
}