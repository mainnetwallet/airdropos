import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import Modal from '@/components/live/Modal'
import { liveApi as api } from '@/services/api'
import type { IntegrationInfo, LiveSession } from '@/types/live'

export default function NewSessionModal({
  integrations,
  onClose,
  onCreated,
}: {
  integrations: IntegrationInfo[]
  onClose: () => void
  onCreated: (session: LiveSession) => void
}) {
  const qc = useQueryClient()
  const profiles = useQuery({ queryKey: ['browser-profiles'], queryFn: api.browserProfiles })
  const free = (profiles.data?.profiles ?? []).filter((p) => p.session_status == null || p.session_status === 'stopped')
  const [profileId, setProfileId] = useState('')
  const [url, setUrl] = useState('')

  const start = useMutation({
    mutationFn: () => api.liveCreateSession(profileId || free[0]?.id || '', url.trim() || undefined),
    onSuccess: ({ session }) => {
      void qc.invalidateQueries({ queryKey: ['live'] })
      onCreated(session)
    },
  })

  const selected = profileId || free[0]?.id || ''
  const others = integrations.filter((i) => i.id !== 'browser' && i.id !== 'telegram')
  const hasTelegram = integrations.some((i) => i.id === 'telegram')

  const tgAccounts = useQuery({ queryKey: ['live-telegram-accounts'], queryFn: api.liveTelegramAccounts, enabled: hasTelegram, retry: false })
  const accounts = (tgAccounts.data?.accounts ?? []).filter((a) => a.session_ready)
  const [account, setAccount] = useState('')
  const tgSelected = account || accounts.find((a) => a.is_default)?.label || accounts[0]?.label || ''
  const openTg = useMutation({
    mutationFn: () => api.liveOpenTelegram(tgSelected),
    onSuccess: ({ session }) => {
      void qc.invalidateQueries({ queryKey: ['live'] })
      void qc.invalidateQueries({ queryKey: ['live-telegram-accounts'] })
      onCreated(session)
    },
  })

  return (
    <Modal title="New session" onClose={onClose}>
      <div className="space-y-4">
        <div>
          <p className="mb-1 text-sm font-semibold text-slate-200">Browser session</p>
          <p className="mb-3 text-xs text-slate-500">
            Opens a real browser from one of your profiles. Cookies and logins are kept per profile.
          </p>
          {profiles.isLoading ? (
            <p className="text-sm text-slate-500">Loading profiles…</p>
          ) : free.length === 0 ? (
            <p className="rounded-lg bg-surface-200 p-3 text-sm text-slate-400">
              {(profiles.data?.profiles.length ?? 0) === 0 ? 'You have no browser profiles yet. ' : 'Every profile already has a running session. '}
              <Link to="/browser-profiles" className="font-semibold text-accent" onClick={onClose}>
                Manage browser profiles
              </Link>
            </p>
          ) : (
            <div className="space-y-3">
              <label className="block text-xs font-medium text-slate-400">
                Profile
                <select className="input mt-1 w-full" value={selected} onChange={(e) => setProfileId(e.target.value)}>
                  {free.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-xs font-medium text-slate-400">
                Start URL (optional)
                <input
                  className="input mt-1 w-full"
                  placeholder="https://"
                  inputMode="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </label>
              {start.error && <p className="text-sm text-status-error">{(start.error as Error).message}</p>}
              <button
                type="button"
                disabled={start.isPending || !selected}
                onClick={() => start.mutate()}
                className="btn btn-primary w-full"
              >
                {start.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null} Start browser session
              </button>
            </div>
          )}
        </div>

        {hasTelegram && (
          <div className="border-t border-surface-300 pt-3">
            <p className="mb-1 text-sm font-semibold text-slate-200">Telegram account</p>
            <p className="mb-3 text-xs text-slate-500">
              Opens a logged-in Telegram account as a live chat view you can read and reply in. The agent does not have to use it first.
            </p>
            {tgAccounts.isLoading ? (
              <p className="text-sm text-slate-500">Loading accounts…</p>
            ) : tgAccounts.isError ? (
              <p className="rounded-lg bg-surface-200 p-3 text-sm text-status-error">
                {(tgAccounts.error as Error).message}
              </p>
            ) : accounts.length === 0 ? (
              <p className="rounded-lg bg-surface-200 p-3 text-sm text-slate-400">
                No logged-in Telegram account yet. Run <span className="font-mono">python scripts/telegram_login.py</span> once in a terminal.
              </p>
            ) : (
              <div className="space-y-3">
                <label className="block text-xs font-medium text-slate-400">
                  Account
                  <select className="input mt-1 w-full" value={tgSelected} onChange={(e) => setAccount(e.target.value)}>
                    {accounts.map((a) => (
                      <option key={a.label} value={a.label}>
                        {a.name || a.label}
                        {a.username ? ` (@${a.username})` : ''}
                        {a.is_default ? ' · default' : ''}
                        {a.open ? ' · open' : ''}
                      </option>
                    ))}
                  </select>
                </label>
                {openTg.error && <p className="text-sm text-status-error">{(openTg.error as Error).message}</p>}
                <button type="button" disabled={openTg.isPending || !tgSelected} onClick={() => openTg.mutate()} className="btn btn-primary w-full">
                  {openTg.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null} Open Telegram account
                </button>
              </div>
            )}
          </div>
        )}

        <div className="border-t border-surface-300 pt-3">
          <p className="mb-2 text-sm font-semibold text-slate-200">Other integrations</p>
          <ul className="space-y-1.5 text-xs text-slate-500">
            {others.map((i) => (
              <li key={i.id}>
                <span className="font-semibold text-slate-300">{i.label}:</span>{' '}
                {i.id === 'telegram'
                  ? 'a session appears automatically after the agent first uses the Telegram account. It cannot be started manually.'
                  : i.note ?? 'Not available yet.'}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Modal>
  )
}
