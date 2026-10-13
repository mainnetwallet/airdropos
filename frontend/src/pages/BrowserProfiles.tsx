/**
 * Browser Profiles: ONE management page for many profiles.
 *
 * A profile is configuration + a real Chrome profile directory. It is not a running
 * browser: "Open Browser" starts (or reconnects to) the real Chrome and the card then
 * shows what the BACKEND reports. Nothing on this page is optimistic: if the worker is
 * unreachable the status says "unknown", never "running".
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Globe,
  Link2,
  Loader2,
  Pencil,
  Play,
  Plus,
  Power,
  Radio,
  RefreshCw,
  Star,
  Trash2,
  UserX,
} from 'lucide-react'
import { Card, EmptyState, ErrorState, Loading, PageHeader, Pill } from '@/components/ui'
import Modal from '@/components/live/Modal'
import { liveApi as api } from '@/services/api'
import type { BrowserProfileInfo, BrowserProfileInput } from '@/types/live'

const EMPTY: BrowserProfileInput = {
  name: '',
  description: '',
  viewport_width: 0,
  viewport_height: 0,
  locale: '',
  timezone_id: '',
  start_url: '',
  browser_type: 'chrome',
  launch_mode: 'managed',
  executable_path: '',
  user_data_dir: '',
  headless: null,
  debugger_host: '',
  debugger_port: 0,
  unpacked_extensions: '',
}

function ProfileForm({
  initial,
  title,
  defaultDirHint,
  submitLabel,
  onClose,
  onSubmit,
  pending,
  error,
}: {
  initial: BrowserProfileInput
  title: string
  defaultDirHint: string
  submitLabel: string
  onClose: () => void
  onSubmit: (value: BrowserProfileInput) => void
  pending: boolean
  error: unknown
}) {
  const [v, setV] = useState(initial)
  const set = <K extends keyof BrowserProfileInput>(k: K, value: BrowserProfileInput[K]) =>
    setV((p) => ({ ...p, [k]: value }))
  const num = (s: string) => (s === '' ? 0 : Number(s) || 0)
  const attach = v.launch_mode === 'attach'
  return (
    <Modal title={title} onClose={onClose}>
      <form
        className="max-h-[70vh] space-y-3 overflow-y-auto"
        onSubmit={(e) => {
          e.preventDefault()
          onSubmit({ ...v, name: v.name.trim() })
        }}
      >
        <label className="block text-xs font-medium text-slate-400">
          Name
          <input className="input mt-1 w-full" required maxLength={60} value={v.name} onChange={(e) => set('name', e.target.value)} placeholder="e.g. Airdrop Profile 1" />
        </label>
        <label className="block text-xs font-medium text-slate-400">
          Description
          <input className="input mt-1 w-full" maxLength={200} value={v.description} onChange={(e) => set('description', e.target.value)} />
        </label>

        <fieldset className="rounded-lg border border-surface-200 p-3">
          <legend className="px-1 text-xs font-semibold text-slate-300">Browser mode</legend>
          <div className="space-y-2">
            <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-300">
              <input
                type="radio"
                name="launch_mode"
                className="mt-0.5"
                checked={!attach}
                onChange={() => setV((p) => ({ ...p, launch_mode: 'managed' }))}
              />
              <span>
                <span className="font-semibold text-slate-200">Mode A — Managed persistent Chrome (default)</span>
                <span className="block text-[11px] text-slate-500">
                  AirdropOS launches the installed Google Chrome with its own profile directory. Cookies,
                  logins and installed extensions are reused on every later task, including after a restart.
                </span>
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-300">
              <input
                type="radio"
                name="launch_mode"
                className="mt-0.5"
                checked={attach}
                onChange={() => setV((p) => ({ ...p, launch_mode: 'attach' }))}
              />
              <span>
                <span className="font-semibold text-slate-200">Mode B — Connect to an existing Chrome</span>
                <span className="block text-[11px] text-slate-500">
                  Attaches to a Chrome you already started yourself with a remote debugging port. Only
                  127.0.0.1 is allowed; that port is unauthenticated, so it must never be exposed to a network.
                </span>
              </span>
            </label>
          </div>
        </fieldset>

        <div className="grid grid-cols-2 gap-3">
          <label className="block text-xs font-medium text-slate-400">
            Browser type
            <select className="input mt-1 w-full" value={v.browser_type} onChange={(e) => set('browser_type', e.target.value as BrowserProfileInput['browser_type'])}>
              <option value="chrome">Google Chrome (installed)</option>
              <option value="chromium">Playwright Chromium (no extensions)</option>
            </select>
          </label>
          <label className="block text-xs font-medium text-slate-400">
            Chrome executable (optional)
            <input
              className="input mt-1 w-full"
              placeholder="Auto-detected"
              value={v.executable_path}
              onChange={(e) => set('executable_path', e.target.value)}
            />
          </label>
        </div>

        {!attach && (
          <>
            <label className="block text-xs font-medium text-slate-400">
              Chrome profile directory (optional)
              <input
                className="input mt-1 w-full"
                placeholder={defaultDirHint}
                value={v.user_data_dir}
                onChange={(e) => set('user_data_dir', e.target.value)}
              />
              <span className="mt-1 block text-[11px] text-slate-500">
                Leave empty for the dedicated default directory. A real Chrome/Chromium profile (your own
                browser data) is refused. Environment variables such as %LOCALAPPDATA% are resolved on the server.
              </span>
            </label>
            <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-300">
              <input type="checkbox" checked={v.headless === true} onChange={(e) => set('headless', e.target.checked ? true : null)} />
              Run headless
              <span className="text-[11px] text-slate-500">
                Off by default: Chrome extensions only work reliably in a real window.
              </span>
            </label>
            <label className="block text-xs font-medium text-slate-400">
              Unpacked developer extensions (optional, comma separated)
              <input
                className="input mt-1 w-full"
                placeholder="C:\ext\my-extension"
                value={v.unpacked_extensions}
                onChange={(e) => set('unpacked_extensions', e.target.value)}
              />
              <span className="mt-1 block text-[11px] text-slate-500">
                Each folder must contain a manifest.json. Chrome loads these for the session only; it does not
                record them as installed, so they disappear after a restart.
              </span>
            </label>
          </>
        )}

        {attach && (
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-xs font-medium text-slate-400">
              DevTools host
              <input className="input mt-1 w-full" placeholder="127.0.0.1" value={v.debugger_host} onChange={(e) => set('debugger_host', e.target.value)} />
            </label>
            <label className="block text-xs font-medium text-slate-400">
              DevTools port
              <input className="input mt-1 w-full" inputMode="numeric" placeholder="9222" value={v.debugger_port || ''} onChange={(e) => set('debugger_port', num(e.target.value))} />
            </label>
            <p className="col-span-2 text-[11px] text-slate-500">
              Start Chrome yourself with <span className="font-mono">--remote-debugging-port=9222</span>. AirdropOS
              never launches a competing Chrome against a profile that is already in use, and disconnecting
              leaves your browser open.
            </p>
          </div>
        )}

        <label className="block text-xs font-medium text-slate-400">
          Startup URL (optional)
          <input className="input mt-1 w-full" inputMode="url" placeholder="https://" value={v.start_url} onChange={(e) => set('start_url', e.target.value)} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-xs font-medium text-slate-400">
            Viewport width
            <input className="input mt-1 w-full" inputMode="numeric" placeholder="default" value={v.viewport_width || ''} onChange={(e) => set('viewport_width', num(e.target.value))} />
          </label>
          <label className="block text-xs font-medium text-slate-400">
            Viewport height
            <input className="input mt-1 w-full" inputMode="numeric" placeholder="default" value={v.viewport_height || ''} onChange={(e) => set('viewport_height', num(e.target.value))} />
          </label>
          <label className="block text-xs font-medium text-slate-400">
            Locale
            <input className="input mt-1 w-full" placeholder="en-US" value={v.locale} onChange={(e) => set('locale', e.target.value)} />
          </label>
          <label className="block text-xs font-medium text-slate-400">
            Timezone
            <input className="input mt-1 w-full" placeholder="Europe/Berlin" value={v.timezone_id} onChange={(e) => set('timezone_id', e.target.value)} />
          </label>
        </div>
        <p className="text-xs text-slate-500">Changes apply the next time a session is started from this profile.</p>
        {error ? <p className="text-sm text-status-error">{(error as Error).message}</p> : null}
        <button type="submit" disabled={pending || !v.name.trim()} className="btn btn-primary w-full">
          {pending && <Loader2 className="h-4 w-4 animate-spin" />} {submitLabel}
        </button>
      </form>
    </Modal>
  )
}

function ExtensionsModal({ profile, onClose }: { profile: BrowserProfileInfo; onClose: () => void }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['browser-profile-extensions', profile.id],
    queryFn: () => api.browserProfileExtensions(profile.id),
    staleTime: 5000,
  })
  const caps = (data?.capabilities ?? {}) as Record<string, unknown>
  return (
    <Modal title={`Extensions — ${profile.name}`} onClose={onClose}>
      <div className="max-h-[70vh] space-y-3 overflow-y-auto">
        <p className="text-xs text-slate-400">
          Read from the Chrome profile's own state at <span className="font-mono break-all">{profile.resolved_user_data_dir}</span>.
        </p>
        {isLoading ? (
          <Loading label="Reading installed extensions" />
        ) : error ? (
          <ErrorState error={error} />
        ) : !data || data.installed.length === 0 ? (
          <p className="text-sm text-slate-400">
            No extensions are installed in this profile yet. Use “Extension settings” to open Chrome's
            extensions page and install one; Chrome will ask you to confirm it yourself.
          </p>
        ) : (
          <ul className="space-y-2">
            {data.installed.map((e) => (
              <li key={e.id} className="rounded-lg bg-surface-200 p-2 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold text-slate-200">{e.name || e.id}</span>
                  <Pill tone={e.enabled ? 'green' : 'gray'}>{e.enabled ? 'Enabled' : 'Disabled'}</Pill>
                </div>
                <p className="mt-1 break-all font-mono text-[10px] text-slate-500">{e.id}</p>
                <p className="text-[11px] text-slate-500">
                  v{e.version || '?'}
                  {e.from_webstore ? ' · Chrome Web Store' : ''}
                </p>
              </li>
            ))}
          </ul>
        )}
        <div className="rounded-lg bg-surface-200 p-2 text-[11px] text-slate-400">
          <p><span className="font-semibold text-slate-300">What is and isn't automated:</span></p>
          <ul className="mt-1 list-disc pl-4">
            <li>Detecting installed extensions: supported.</li>
            <li>Opening an extension's own page: supported.</li>
            <li>Installing from the Web Store: Chrome requires a manual “Add to Chrome” confirmation.</li>
            <li>Reading an extension popup rendered in Chrome's own window: not accessible to any web automation.</li>
          </ul>
          {typeof caps.notes === 'string' ? <p className="mt-2">{caps.notes}</p> : null}
        </div>
      </div>
    </Modal>
  )
}

function DeleteDataModal({ profile, onClose, onDone }: {
  profile: BrowserProfileInfo
  onClose: () => void
  onDone: (message: string) => void
}) {
  const [confirm, setConfirm] = useState('')
  const wipe = useMutation({
    mutationFn: () => api.deleteBrowserProfileData(profile.id),
    onSuccess: (r) => {
      onClose()
      onDone(
        r.deleted
          ? `Deleted the Chrome profile data at ${r.path}. Its cookies, logins and extensions are gone.`
          : `Nothing was deleted: ${r.path} did not exist.`,
      )
    },
  })
  return (
    <Modal title={`Delete Chrome profile data — ${profile.name}`} onClose={onClose}>
      <div className="space-y-3">
        <p className="flex gap-2 rounded-lg bg-red-500/10 p-3 text-xs text-red-300">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>
            This permanently deletes <span className="font-mono break-all">{profile.resolved_user_data_dir}</span>:
            every cookie, saved login, extension and site preference in it. It cannot be undone. Deleting the
            profile configuration is a separate, safer action.
          </span>
        </p>
        <label className="block text-xs font-medium text-slate-400">
          Type the profile id to confirm: <span className="font-mono text-slate-200">{profile.id}</span>
          <input className="input mt-1 w-full font-mono" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </label>
        {wipe.error ? <p className="text-sm text-status-error">{(wipe.error as Error).message}</p> : null}
        <button
          type="button"
          disabled={confirm !== profile.id || wipe.isPending}
          onClick={() => wipe.mutate()}
          className="btn w-full bg-red-500/20 text-red-300 disabled:opacity-50"
        >
          {wipe.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Permanently delete Chrome profile data
        </button>
      </div>
    </Modal>
  )
}

export default function BrowserProfiles() {
  const qc = useQueryClient()
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['browser-profiles'],
    queryFn: api.browserProfiles,
    refetchInterval: 8000,
  })
  const [editing, setEditing] = useState<BrowserProfileInfo | 'new' | null>(null)
  const [extensionsFor, setExtensionsFor] = useState<BrowserProfileInfo | null>(null)
  const [wipeFor, setWipeFor] = useState<BrowserProfileInfo | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: ['browser-profiles'] })
  const fail = (e: unknown) => setNotice(e instanceof Error ? e.message : 'Action failed')

  const save = useMutation({
    mutationFn: (value: BrowserProfileInput) =>
      editing && editing !== 'new' ? api.updateBrowserProfile(editing.id, value) : api.createBrowserProfile(value),
    onSuccess: () => { setEditing(null); setNotice(null); void refresh() },
  })
  const open = useMutation({
    mutationFn: (p: BrowserProfileInfo) => api.openBrowserProfile(p.id),
    onSuccess: ({ profile, reused }) => {
      setNotice(
        reused
          ? `Already running: ${profile.active_title || profile.active_url || profile.name}.`
          : `Chrome started for "${profile.name}" using ${profile.resolved_user_data_dir}.`,
      )
      void qc.invalidateQueries({ queryKey: ['live'] })
      void refresh()
    },
    onError: fail,
  })
  const reconnect = useMutation({
    mutationFn: (p: BrowserProfileInfo) => api.reconnectBrowserProfile(p.id),
    onSuccess: ({ profile }) => {
      setNotice(`Reconnected to "${profile.name}". ${profile.last_recovery || profile.connection || ''}`.trim())
      void refresh()
    },
    onError: fail,
  })
  const stop = useMutation({
    mutationFn: (p: BrowserProfileInfo) => api.closeBrowserProfile(p.id),
    onSuccess: (r) => {
      setNotice(r.was_running ? 'Browser stopped. The Chrome profile data was kept on disk.' : 'That browser was not running.')
      void refresh()
    },
    onError: fail,
  })
  const makeDefault = useMutation({
    mutationFn: (id: string | null) => api.setDefaultBrowserProfile(id),
    onSuccess: (r) => {
      setNotice(r.default_agent_profile ? 'This profile is now the agent default.' : 'No default agent profile is set.')
      void refresh()
    },
    onError: fail,
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteBrowserProfile(id),
    onSuccess: (r) => {
      setNotice(`Profile configuration deleted. Its Chrome data was kept at ${r.chrome_data_kept_at}.`)
      void refresh()
    },
    onError: fail,
  })
  const clearState = useMutation({
    mutationFn: (id: string) => api.clearBrowserProfileState(id),
    onSuccess: () => { setNotice('Saved session snapshot cleared.'); void refresh() },
    onError: fail,
  })

  if (isLoading) return <Loading label="Loading browser profiles" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />
  const profiles = data?.profiles ?? []
  const chromeInfo = data?.chrome
  const dirHint = data?.profile_root ? `${data.profile_root}\\<profile-id>` : ''

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title="Browser Profiles"
        subtitle="Persistent Google Chrome profiles. Open one, use it for agent tasks, and it is still the same browser — with the same logins and extensions — next time."
        action={
          <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>
            <Plus className="h-4 w-4" /> New profile
          </button>
        }
      />

      {chromeInfo && (
        <div
          className={`rounded-lg px-3 py-2 text-xs ${chromeInfo.found ? 'bg-surface-200 text-slate-300' : 'bg-amber-500/10 text-amber-300'}`}
        >
          {chromeInfo.found ? (
            <>
              Google Chrome {chromeInfo.version || ''} detected at{' '}
              <span className="font-mono break-all">{chromeInfo.path}</span>. Profile root:{' '}
              <span className="font-mono break-all">{data?.profile_root}</span>.
              {!data?.persistent_profiles_enabled && ' Persistent profiles are disabled, so sessions fall back to a temporary browser.'}
            </>
          ) : (
            <>
              Google Chrome was not found, so managed profiles cannot be launched. {chromeInfo.detail}
            </>
          )}
          {!data?.worker_reachable && (
            <span className="mt-1 block text-amber-300">
              The browser worker is unreachable, so every status below is “unknown”, not “stopped”.
            </span>
          )}
        </div>
      )}

      {notice && <p className="rounded-lg bg-surface-200 px-3 py-2 text-sm text-slate-300">{notice}</p>}

      {profiles.length === 0 ? (
        <Card>
          <EmptyState
            title="No browser profiles yet"
            description="Create one to get a dedicated Google Chrome profile that keeps its cookies, logins and installed extensions between tasks and across restarts."
            icon={<Globe className="h-8 w-8" />}
          />
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {profiles.map((p) => {
            const unknown = p.session_status === 'unknown'
            const running = p.browser_running && !unknown
            const locked = running || unknown // never change a profile whose real state is not confirmed
            const attached = p.launch_mode === 'attach'
            return (
              <Card key={p.id} className="flex flex-col gap-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="flex items-center gap-1 truncate font-bold text-slate-100">
                      {p.is_default_agent && <Star className="h-3.5 w-3.5 shrink-0 text-amber-400" aria-label="Default agent profile" />}
                      <span className="truncate">{p.name}</span>
                    </p>
                    <p className="truncate text-xs text-slate-500">{p.description || 'No description'}</p>
                  </div>
                  {running ? (
                    <Link to={`/live/browser/${encodeURIComponent(p.id)}`}>
                      <Pill tone="green"><span className="inline-flex items-center gap-1"><Radio className="h-3 w-3" /> Running</span></Pill>
                    </Link>
                  ) : unknown ? (
                    <Pill tone="gray">Status unknown</Pill>
                  ) : (
                    <Pill tone="gray">Stopped</Pill>
                  )}
                </div>

                <dl className="space-y-0.5 text-[11px] text-slate-400">
                  <div className="flex gap-1">
                    <dt className="shrink-0 text-slate-500">Mode:</dt>
                    <dd>{attached ? 'Connect to existing Chrome' : 'Managed persistent Chrome'}</dd>
                  </div>
                  <div className="flex gap-1">
                    <dt className="shrink-0 text-slate-500">Browser:</dt>
                    <dd className="truncate">{p.browser_type === 'chrome' ? p.resolved_executable || 'Google Chrome (not found)' : 'Playwright Chromium'}</dd>
                  </div>
                  <div className="flex gap-1">
                    <dt className="shrink-0 text-slate-500">Directory:</dt>
                    <dd className="truncate font-mono text-[10px]" title={p.resolved_user_data_dir}>{p.resolved_user_data_dir}</dd>
                  </div>
                  <div className="flex gap-1">
                    <dt className="shrink-0 text-slate-500">Tabs:</dt>
                    <dd>{p.tab_count ?? 0}{p.active_title ? ` · ${p.active_title.slice(0, 40)}` : ''}</dd>
                  </div>
                  <div className="flex gap-1">
                    <dt className="shrink-0 text-slate-500">Extensions:</dt>
                    <dd>{p.extension_count == null ? 'unknown' : p.extension_count}</dd>
                  </div>
                  <div className="flex gap-1">
                    <dt className="shrink-0 text-slate-500">Last used:</dt>
                    <dd>{p.last_used_at ? new Date(p.last_used_at).toLocaleString() : 'never'}</dd>
                  </div>
                </dl>

                {p.configuration_problems.length > 0 && (
                  <p className="rounded-md bg-amber-500/10 px-2 py-1 text-[11px] text-amber-300">
                    {p.configuration_problems.join(' ')}
                  </p>
                )}
                {p.last_error && !running && (
                  <p className="rounded-md bg-red-500/10 px-2 py-1 text-[11px] text-red-300">{p.last_error}</p>
                )}
                {p.last_recovery && (
                  <p className="rounded-md bg-surface-200 px-2 py-1 text-[11px] text-slate-400">Recovery: {p.last_recovery}</p>
                )}

                <div className="flex flex-wrap gap-1.5 text-[11px] text-slate-400">
                  <span className="rounded-md bg-surface-200 px-2 py-0.5">
                    {p.viewport_width && p.viewport_height ? `${p.viewport_width}×${p.viewport_height}` : 'Default viewport'}
                  </span>
                  {p.locale && <span className="rounded-md bg-surface-200 px-2 py-0.5">{p.locale}</span>}
                  {p.headless ? <span className="rounded-md bg-surface-200 px-2 py-0.5">Headless</span> : null}
                  {p.is_default_agent && <span className="rounded-md bg-amber-500/10 px-2 py-0.5 text-amber-300">Agent default</span>}
                </div>

                <div className="mt-auto flex flex-wrap gap-2">
                  {!running ? (
                    <button type="button" disabled={locked || open.isPending} onClick={() => open.mutate(p)} className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-accent px-3 text-xs font-semibold text-white disabled:opacity-50">
                      {open.isPending && open.variables?.id === p.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />} Open Browser
                    </button>
                  ) : (
                    <button type="button" disabled={stop.isPending} onClick={() => stop.mutate(p)} className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200 disabled:opacity-50">
                      {stop.isPending && stop.variables?.id === p.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Power className="h-3.5 w-3.5" />} Stop Browser
                    </button>
                  )}
                  <button type="button" disabled={unknown || reconnect.isPending} onClick={() => reconnect.mutate(p)} className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200 disabled:opacity-50">
                    {reconnect.isPending && reconnect.variables?.id === p.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Reconnect
                  </button>
                  <button
                    type="button"
                    disabled={makeDefault.isPending || p.launch_mode === 'attach'}
                    title={p.launch_mode === 'attach' ? 'An attached browser is not reused for agent tasks' : undefined}
                    onClick={() => makeDefault.mutate(p.is_default_agent ? null : p.id)}
                    className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200 disabled:opacity-50"
                  >
                    <Star className="h-3.5 w-3.5" /> {p.is_default_agent ? 'Unset default' : 'Set as agent default'}
                  </button>
                  <button type="button" onClick={() => setExtensionsFor(p)} className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200">
                    <Link2 className="h-3.5 w-3.5" /> Extensions
                  </button>
                  <button type="button" onClick={() => setEditing(p)} className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200">
                    <Pencil className="h-3.5 w-3.5" /> Edit
                  </button>
                  <button
                    type="button"
                    disabled={!p.has_saved_state || locked}
                    onClick={() => window.confirm(`Forget the saved session snapshot for "${p.name}"? This does not touch a managed Chrome profile.`) && clearState.mutate(p.id)}
                    className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200 disabled:opacity-50"
                  >
                    <UserX className="h-3.5 w-3.5" /> Clear snapshot
                  </button>
                  <button
                    type="button"
                    disabled={locked}
                    onClick={() => window.confirm(`Delete the profile CONFIGURATION for "${p.name}"? Its Chrome profile data (cookies, logins, extensions) is kept on disk.`) && remove.mutate(p.id)}
                    className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200 disabled:opacity-50"
                  >
                    <Trash2 className="h-3.5 w-3.5" /> Delete config
                  </button>
                  <button
                    type="button"
                    disabled={locked || attached}
                    title={attached ? 'AirdropOS does not own the data of a Chrome you started yourself' : undefined}
                    onClick={() => setWipeFor(p)}
                    className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-red-500/10 px-3 text-xs font-semibold text-red-600 disabled:opacity-50"
                  >
                    <AlertTriangle className="h-3.5 w-3.5" /> Delete data
                  </button>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      <p className="px-1 text-xs text-slate-500">
        Each managed profile is a real Chrome profile directory, so cookies, logins and installed extensions are
        reused by later agent tasks and survive an AirdropOS restart. Agent browser tasks use the profile marked as
        the default unless the task names another one. Deleting a profile configuration never deletes its Chrome
        data; that is a separate, explicitly confirmed action.
      </p>

      {editing && (
        <ProfileForm
          title={editing === 'new' ? 'New browser profile' : 'Edit browser profile'}
          submitLabel={editing === 'new' ? 'Save profile' : 'Save changes'}
          defaultDirHint={dirHint}
          initial={editing === 'new' ? EMPTY : {
            name: editing.name, description: editing.description, viewport_width: editing.viewport_width,
            viewport_height: editing.viewport_height, locale: editing.locale, timezone_id: editing.timezone_id,
            start_url: editing.start_url, browser_type: editing.browser_type, launch_mode: editing.launch_mode,
            executable_path: editing.executable_path, user_data_dir: editing.user_data_dir,
            headless: editing.headless ? true : null, debugger_host: editing.debugger_host,
            debugger_port: editing.debugger_port, unpacked_extensions: editing.unpacked_extensions,
          }}
          onClose={() => setEditing(null)}
          onSubmit={(v) => save.mutate(v)}
          pending={save.isPending}
          error={save.error}
        />
      )}
      {extensionsFor && <ExtensionsModal profile={extensionsFor} onClose={() => setExtensionsFor(null)} />}
      {wipeFor && (
        <DeleteDataModal
          profile={wipeFor}
          onClose={() => setWipeFor(null)}
          onDone={(m) => { setNotice(m); void refresh() }}
        />
      )}
    </div>
  )
}
