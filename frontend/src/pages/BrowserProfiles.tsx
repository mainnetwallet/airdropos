/**
 * Browser Profiles: ONE management page for many profiles.
 * A profile is configuration + saved (encrypted) session state. It is not a running browser:
 * running sessions are started from a profile and appear on Live Broadcast.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Globe, Loader2, Pencil, Play, Plus, Radio, Trash2, UserX } from 'lucide-react'
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
}

function ProfileForm({
  initial,
  title,
  onClose,
  onSubmit,
  pending,
  error,
}: {
  initial: BrowserProfileInput
  title: string
  onClose: () => void
  onSubmit: (value: BrowserProfileInput) => void
  pending: boolean
  error: unknown
}) {
  const [v, setV] = useState(initial)
  const set = <K extends keyof BrowserProfileInput>(k: K, value: BrowserProfileInput[K]) => setV((p) => ({ ...p, [k]: value }))
  const num = (s: string) => (s === '' ? 0 : Number(s) || 0)
  return (
    <Modal title={title} onClose={onClose}>
      <form
        className="space-y-3"
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
        <label className="block text-xs font-medium text-slate-400">
          Start URL (optional)
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
          {pending && <Loader2 className="h-4 w-4 animate-spin" />} Save profile
        </button>
      </form>
    </Modal>
  )
}

export default function BrowserProfiles() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['browser-profiles'],
    queryFn: api.browserProfiles,
    refetchInterval: 8000,
  })
  const [editing, setEditing] = useState<BrowserProfileInfo | 'new' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: ['browser-profiles'] })
  const fail = (e: unknown) => setNotice(e instanceof Error ? e.message : 'Action failed')

  const save = useMutation({
    mutationFn: (value: BrowserProfileInput) =>
      editing && editing !== 'new' ? api.updateBrowserProfile(editing.id, value) : api.createBrowserProfile(value),
    onSuccess: () => { setEditing(null); setNotice(null); void refresh() },
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteBrowserProfile(id),
    onSuccess: () => { setNotice(null); void refresh() },
    onError: fail,
  })
  const clearState = useMutation({
    mutationFn: (id: string) => api.clearBrowserProfileState(id),
    onSuccess: () => { setNotice('Saved session cleared.'); void refresh() },
    onError: fail,
  })
  const start = useMutation({
    mutationFn: (p: BrowserProfileInfo) => api.liveCreateSession(p.id),
    onSuccess: ({ session }) => {
      void qc.invalidateQueries({ queryKey: ['live'] })
      navigate(`/live/browser/${encodeURIComponent(session.key)}`)
    },
    onError: fail,
  })

  if (isLoading) return <Loading label="Loading browser profiles" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />
  const profiles = data?.profiles ?? []

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title="Browser Profiles"
        subtitle="Create and manage isolated browser identities. Start sessions from them; watch them on Live Broadcast."
        action={
          <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>
            <Plus className="h-4 w-4" /> New profile
          </button>
        }
      />

      {notice && <p className="rounded-lg bg-surface-200 px-3 py-2 text-sm text-slate-300">{notice}</p>}

      {profiles.length === 0 ? (
        <Card>
          <EmptyState
            title="No browser profiles yet"
            description="A profile keeps its own cookies and logins, separate from every other profile."
            icon={<Globe className="h-8 w-8" />}
          />
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {profiles.map((p) => {
            const running = p.session_status != null && p.session_status !== 'stopped'
            return (
              <Card key={p.id} className="flex flex-col gap-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate font-bold text-slate-100">{p.name}</p>
                    <p className="truncate text-xs text-slate-500">{p.description || 'No description'}</p>
                  </div>
                  {running ? (
                    <Link to={`/live/browser/${encodeURIComponent(p.id)}`}>
                      <Pill tone="green"><span className="inline-flex items-center gap-1"><Radio className="h-3 w-3" /> Running</span></Pill>
                    </Link>
                  ) : (
                    <Pill tone="gray">Not running</Pill>
                  )}
                </div>
                <div className="flex flex-wrap gap-1.5 text-[11px] text-slate-400">
                  <span className="rounded-md bg-surface-200 px-2 py-0.5">
                    {p.viewport_width && p.viewport_height ? `${p.viewport_width}×${p.viewport_height}` : 'Default viewport'}
                  </span>
                  {p.locale && <span className="rounded-md bg-surface-200 px-2 py-0.5">{p.locale}</span>}
                  {p.timezone_id && <span className="rounded-md bg-surface-200 px-2 py-0.5">{p.timezone_id}</span>}
                  <span className="rounded-md bg-surface-200 px-2 py-0.5">{p.has_saved_state ? 'Saved session' : 'No saved session'}</span>
                </div>
                {p.start_url && <p className="truncate text-xs text-slate-500">Start: {p.start_url}</p>}
                <div className="mt-auto flex flex-wrap gap-2">
                  <button type="button" disabled={running || start.isPending} onClick={() => start.mutate(p)} className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-accent px-3 text-xs font-semibold text-white disabled:opacity-50">
                    {start.isPending && start.variables?.id === p.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />} Start session
                  </button>
                  <button type="button" onClick={() => setEditing(p)} className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200">
                    <Pencil className="h-3.5 w-3.5" /> Edit
                  </button>
                  <button
                    type="button"
                    disabled={!p.has_saved_state || running}
                    onClick={() => window.confirm(`Forget saved cookies for "${p.name}"? Every site will be logged out for this profile.`) && clearState.mutate(p.id)}
                    className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-surface-200 px-3 text-xs font-semibold text-slate-200 disabled:opacity-50"
                  >
                    <UserX className="h-3.5 w-3.5" /> Clear login
                  </button>
                  <button
                    type="button"
                    disabled={running}
                    onClick={() => window.confirm(`Delete profile "${p.name}" and its saved session?`) && remove.mutate(p.id)}
                    className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-red-500/10 px-3 text-xs font-semibold text-red-600 disabled:opacity-50"
                  >
                    <Trash2 className="h-3.5 w-3.5" /> Delete
                  </button>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      <p className="px-1 text-xs text-slate-500">
        Cookies and local storage are saved encrypted on the server so a profile stays logged in across restarts. The
        agent uses a profile through the same <span className="font-mono">session_key</span>, so manual logins carry over to tasks.
      </p>

      {editing && (
        <ProfileForm
          title={editing === 'new' ? 'New browser profile' : 'Edit browser profile'}
          initial={editing === 'new' ? EMPTY : {
            name: editing.name, description: editing.description, viewport_width: editing.viewport_width,
            viewport_height: editing.viewport_height, locale: editing.locale, timezone_id: editing.timezone_id,
            start_url: editing.start_url,
          }}
          onClose={() => setEditing(null)}
          onSubmit={(v) => save.mutate(v)}
          pending={save.isPending}
          error={save.error}
        />
      )}
    </div>
  )
}
