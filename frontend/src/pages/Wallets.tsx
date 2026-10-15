/**
 * Wallet Manager: create, import, watch, inspect and control wallets.
 *
 * Secret handling: a private key / mnemonic exists only in this component's local
 * state while the import form is open. It is sent once, then cleared. It is never
 * written to browser storage, never placed in a react-query key or mutation (those
 * caches retain their variables), and never logged. The backend never returns it.
 */

import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ChevronDown,
  ChevronUp,
  Eye,
  KeyRound,
  Lock,
  Plus,
  Power,
  RefreshCw,
  Trash2,
  Unlock,
  Upload,
  Wallet as WalletIcon,
  X,
  Star,
} from 'lucide-react'
import { api } from '@/services/api'
import clsx from 'clsx'
import { Alert, Card, Chips, EmptyState, ErrorState, IconTile, Loading } from '@/components/ui'
import type { WalletView } from '@/types'

type FormMode = 'create' | 'import' | 'watch' | null
type WalletFilter = 'all' | 'active' | 'watch'

/** 0x3a4…7f2e: enough to recognise a wallet on a narrow screen. */
function shortAddress(address: string): string {
  return address.length > 14 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address
}

const EMPTY_FORM = { label: '', secret: '', address: '', notes: '' }

function describe(error: unknown): string {
  return error instanceof Error ? error.message : 'Request failed'
}

export default function Wallets() {
  const queryClient = useQueryClient()
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['wallets'],
    queryFn: api.listWallets,
    refetchInterval: 30000,
  })

  const [mode, setMode] = useState<FormMode>(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [filter, setFilter] = useState<WalletFilter>('all')

  // Never leave key material in state after the form goes away.
  useEffect(() => {
    if (mode === null) setForm(EMPTY_FORM)
  }, [mode])
  useEffect(() => () => setForm(EMPTY_FORM), [])

  if (isLoading) return <Loading label="Loading wallets" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />

  const wallets = data?.wallets ?? []
  const visibleWallets = wallets.filter((w) =>
    filter === 'active' ? w.is_active : filter === 'watch' ? !w.has_key_material : true,
  )

  const openForm = (next: Exclude<FormMode, null>) => {
    setMode(mode === next ? null : next)
    setForm(EMPTY_FORM)
    setFormError(null)
    setNotice(null)
  }

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['wallets'] })

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!mode || submitting) return
    setSubmitting(true)
    setFormError(null)
    const notes = form.notes.trim() || undefined
    try {
      let created: WalletView
      if (mode === 'create') {
        created = await api.createWallet({ label: form.label.trim(), notes })
      } else if (mode === 'import') {
        created = await api.importWallet({
          label: form.label.trim(),
          secret: form.secret.trim(),
          notes,
        })
      } else {
        created = await api.watchWallet({
          label: form.label.trim(),
          address: form.address.trim(),
          notes,
        })
      }
      setNotice(`Wallet "${created.label}" added (${created.address}).`)
      setMode(null)
      await refresh()
    } catch (err) {
      setFormError(describe(err))
    } finally {
      // Clear the secret whether the request succeeded or failed.
      setForm((f) => ({ ...f, secret: '' }))
      setSubmitting(false)
    }
  }

  const runAction = async (id: string, action: () => Promise<unknown>) => {
    setBusyId(id)
    setActionError(null)
    try {
      await action()
      await refresh()
    } catch (err) {
      setActionError(describe(err))
    } finally {
      setBusyId(null)
    }
  }

  const handleDelete = (wallet: WalletView) => {
    const warning = wallet.has_key_material
      ? `Permanently delete "${wallet.label}"? Its encrypted key cannot be recovered, and its transactions and claims are deleted too.`
      : `Permanently delete watch-only wallet "${wallet.label}"? Its transactions and claims are deleted too.`
    if (!window.confirm(warning)) return
    void runAction(wallet.id, async () => {
      await api.deleteWallet(wallet.id)
      if (expanded === wallet.id) setExpanded(null)
    })
  }

  const canSubmit =
    form.label.trim().length > 0 &&
    (mode === 'create' ||
      (mode === 'import' && form.secret.trim().length > 0) ||
      (mode === 'watch' && form.address.trim().length > 0))

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex w-full items-start justify-between gap-3 lg:w-auto">
          <div className="min-w-0">
            <h1 className="text-[26px] font-bold leading-tight text-slate-100 lg:text-2xl">
              <span className="lg:hidden">Wallets</span>
              <span className="hidden lg:inline">Wallet Manager</span>
            </h1>
            <p className="mt-1 text-sm text-slate-400">
              <span className="lg:hidden">Manage your wallets securely</span>
              <span className="hidden lg:inline">Configured accounts and their networks</span>
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowAdd((v) => !v)}
            aria-expanded={showAdd}
            className="btn-primary shrink-0 rounded-xl lg:hidden"
          >
            <Plus className="h-4 w-4" />
            Add Wallet
          </button>
        </div>
        <div className={clsx('flex flex-wrap gap-2 max-lg:w-full', !showAdd && 'max-lg:hidden')}>
          <button onClick={() => openForm('create')} className="btn-primary">
            <Plus className="h-4 w-4" />
            Create
          </button>
          <button onClick={() => openForm('import')} className="btn-ghost">
            <Upload className="h-4 w-4" />
            Import
          </button>
          <button onClick={() => openForm('watch')} className="btn-ghost">
            <Eye className="h-4 w-4" />
            Watch-only
          </button>
          <button onClick={() => refetch()} className="btn-ghost" aria-label="Refresh wallets">
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </header>

      <Alert kind="info" title="Secrets are never displayed">
        Private keys and seed phrases are encrypted with AES-256-GCM in the backend vault and are
        never sent back to the browser, the LLM, or the logs. This view shows public addresses
        only. Watch-only addresses hold no key and can never sign.
      </Alert>

      {wallets.length > 0 && (
        <Chips
          className="lg:hidden"
          value={filter}
          onChange={setFilter}
          items={[
            { id: 'all', label: 'All', count: wallets.length },
            { id: 'active', label: 'Active', count: wallets.filter((w) => w.is_active).length },
            { id: 'watch', label: 'Watch-only', count: wallets.filter((w) => !w.has_key_material).length },
          ]}
        />
      )}

      {notice && <Alert kind="success">{notice}</Alert>}
      {actionError && <Alert kind="error">{actionError}</Alert>}

      {mode && (
        <Card
          title={
            mode === 'create'
              ? 'Create wallet'
              : mode === 'import'
                ? 'Import wallet'
                : 'Add watch-only address'
          }
          action={
            <button onClick={() => setMode(null)} className="btn-ghost" aria-label="Close form">
              <X className="h-4 w-4" />
            </button>
          }
        >
          <form onSubmit={submit} autoComplete="off" className="space-y-3">
            {mode === 'create' && (
              <Alert kind="warning" title="Key is generated and kept in the vault">
                A new key is generated on the server and stored encrypted. It is never shown or
                exportable, so this wallet can only be used by the platform. Import an existing
                key instead if you need to keep a copy.
              </Alert>
            )}
            {mode === 'import' && (
              <Alert kind="warning" title="Paste a private key or recovery phrase only on a device you trust">
                It is sent once over your authenticated session, encrypted immediately, and cleared
                from this page. Use a dedicated airdrop wallet, never one that holds significant
                funds.
              </Alert>
            )}

            <label className="block text-sm text-slate-300">
              Label
              <input
                className="input mt-1 w-full"
                value={form.label}
                maxLength={255}
                onChange={(e) => setForm((f) => ({ ...f, label: e.target.value }))}
                placeholder="e.g. Airdrop wallet 1"
                required
              />
            </label>

            {mode === 'import' && (
              <label className="block text-sm text-slate-300">
                Private key (0x…) or recovery phrase
                <input
                  className="input mt-1 w-full font-mono"
                  type="password"
                  name="wallet-secret"
                  value={form.secret}
                  onChange={(e) => setForm((f) => ({ ...f, secret: e.target.value }))}
                  autoComplete="off"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  required
                />
              </label>
            )}

            {mode === 'watch' && (
              <label className="block text-sm text-slate-300">
                Address
                <input
                  className="input mt-1 w-full font-mono"
                  value={form.address}
                  onChange={(e) => setForm((f) => ({ ...f, address: e.target.value }))}
                  placeholder="0x…"
                  maxLength={42}
                  spellCheck={false}
                  required
                />
              </label>
            )}

            <label className="block text-sm text-slate-300">
              Notes (optional)
              <input
                className="input mt-1 w-full"
                value={form.notes}
                onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value }))}
              />
            </label>

            {formError && <Alert kind="error">{formError}</Alert>}

            <div className="flex gap-2">
              <button type="submit" className="btn-primary" disabled={!canSubmit || submitting}>
                {submitting ? 'Saving…' : mode === 'watch' ? 'Add address' : mode === 'import' ? 'Import' : 'Create'}
              </button>
              <button type="button" className="btn-ghost" onClick={() => setMode(null)}>
                Cancel
              </button>
            </div>
          </form>
        </Card>
      )}

      {wallets.length === 0 ? (
        <EmptyState
          title="No wallets configured"
          description="Create a wallet, import a key, or add a watch-only address to enable campaign participation."
          icon={<WalletIcon className="h-8 w-8" />}
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 lg:gap-4">
          {visibleWallets.length === 0 && (
            <p className="col-span-full py-8 text-center text-sm text-slate-500">
              No wallets match this filter.
            </p>
          )}
          {visibleWallets.map((wallet) => {
            const busy = busyId === wallet.id
            const open = expanded === wallet.id
            return (
              <Card key={wallet.id}>
                <div className="flex items-start justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-3">
                    <IconTile tone={wallet.has_key_material ? 'blue' : 'slate'} className="lg:hidden">
                      <WalletIcon />
                    </IconTile>
                    <div className="min-w-0">
                      <h3 className="truncate font-medium text-slate-100">{wallet.label}</h3>
                      <p className="font-mono text-xs text-slate-500">
                        <span className="lg:hidden">{shortAddress(wallet.address)}</span>
                        <span className="hidden lg:inline">{wallet.chain_type}</span>
                      </p>
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-wrap justify-end gap-1">
                    {!wallet.has_key_material && (
                      <span className="badge bg-surface-300 capitalize text-slate-300">watch-only</span>
                    )}
                    {wallet.is_default && (
                      <span className="badge bg-blue-900 text-blue-300">default</span>
                    )}
                    {wallet.is_locked && (
                      <span className="badge bg-yellow-900 text-yellow-300">locked</span>
                    )}
                    <span
                      className={`badge ${
                        wallet.is_active ? 'bg-green-900 text-green-300' : 'bg-slate-800 text-slate-400'
                      }`}
                    >
                      {wallet.is_active ? 'active' : 'inactive'}
                    </span>
                  </div>
                </div>

                <p className="mt-3 break-all rounded bg-surface-200 px-2 py-1.5 font-mono text-xs text-slate-400 max-lg:hidden">
                  {wallet.address}
                </p>

                <div className="mt-3 flex flex-wrap gap-1">
                  {wallet.supported_networks.map((network) => (
                    <span key={network} className="badge bg-surface-300 text-slate-400">
                      {network}
                    </span>
                  ))}
                </div>

                <div className="mt-3 flex justify-between border-t border-surface-300 pt-2 text-xs">
                  <span className="text-slate-500">Total spent</span>
                  <span className="text-status-warning">${wallet.total_spent_usd.toFixed(2)}</span>
                </div>

                <button
                  onClick={() => setExpanded(open ? null : wallet.id)}
                  className="mt-3 flex w-full items-center justify-between text-xs text-slate-400 hover:text-slate-200"
                  aria-expanded={open}
                >
                  Details
                  {open ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                </button>

                {open && (
                  <dl className="mt-2 space-y-1 text-xs">
                    <div className="lg:hidden">
                      <dt className="text-slate-500">Address</dt>
                      <dd className="break-all font-mono text-slate-300">{wallet.address}</dd>
                    </div>
                    <div className="flex justify-between gap-2">
                      <dt className="text-slate-500">Can sign</dt>
                      <dd className="text-slate-300">{wallet.can_sign ? 'yes' : 'no'}</dd>
                    </div>
                    <div className="flex justify-between gap-2">
                      <dt className="text-slate-500">Key stored</dt>
                      <dd className="text-slate-300">
                        {wallet.has_key_material ? 'encrypted in vault' : 'none (watch-only)'}
                      </dd>
                    </div>
                    {wallet.derivation_path && (
                      <div className="flex justify-between gap-2">
                        <dt className="text-slate-500">Derivation path</dt>
                        <dd className="font-mono text-slate-300">{wallet.derivation_path}</dd>
                      </div>
                    )}
                    {wallet.created_at && (
                      <div className="flex justify-between gap-2">
                        <dt className="text-slate-500">Created</dt>
                        <dd className="text-slate-300">{new Date(wallet.created_at).toLocaleString()}</dd>
                      </div>
                    )}
                    {wallet.notes && (
                      <div>
                        <dt className="text-slate-500">Notes</dt>
                        <dd className="break-words text-slate-300">{wallet.notes}</dd>
                      </div>
                    )}
                  </dl>
                )}

                <div className="mt-3 flex flex-wrap gap-2 border-t border-surface-300 pt-3">
                  {wallet.has_key_material && (
                    <button
                      className="btn-ghost"
                      disabled={busy}
                      onClick={() =>
                        void runAction(wallet.id, () => api.setWalletLocked(wallet.id, !wallet.is_locked))
                      }
                    >
                      {wallet.is_locked ? <Unlock className="h-4 w-4" /> : <Lock className="h-4 w-4" />}
                      {wallet.is_locked ? 'Unlock' : 'Lock'}
                    </button>
                  )}
                  {wallet.is_active && (
                    <button
                      className="btn-ghost"
                      disabled={busy}
                      title="The agent uses this wallet when a task asks for an address and names none"
                      onClick={() =>
                        void runAction(wallet.id, () =>
                          api.setWalletDefault(wallet.id, !wallet.is_default),
                        )
                      }
                    >
                      <Star className="h-4 w-4" />
                      {wallet.is_default ? 'Remove default' : 'Set as default'}
                    </button>
                  )}
                  <button
                    className="btn-ghost"
                    disabled={busy}
                    onClick={() =>
                      void runAction(wallet.id, () => api.setWalletActive(wallet.id, !wallet.is_active))
                    }
                  >
                    <Power className="h-4 w-4" />
                    {wallet.is_active ? 'Deactivate' : 'Activate'}
                  </button>
                  <button className="btn-danger" disabled={busy} onClick={() => handleDelete(wallet)}>
                    <Trash2 className="h-4 w-4" />
                    Delete
                  </button>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      <Card title="How signing works">
        <ol className="list-inside list-decimal space-y-2 text-sm text-slate-400">
          <li>The agent formulates a structured signing request containing no secret material.</li>
          <li>The vault independently validates chain, address authorization, calldata size, and forbidden selectors.</li>
          <li>Spend limits and emergency-stop state are checked in the transaction engine.</li>
          <li>Only then is the key decrypted into an ephemeral scope, used, and scrubbed.</li>
        </ol>
        <div className="mt-3 flex items-center gap-2 text-xs text-slate-500">
          <KeyRound className="h-4 w-4" />
          The LLM never sees, requests, or stores key material.
        </div>
      </Card>
    </div>
  )
}
