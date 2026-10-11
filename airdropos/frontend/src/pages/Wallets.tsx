/** Wallet Manager: public addresses, networks, and spend tracking. */

import { useQuery } from '@tanstack/react-query'
import { KeyRound, Wallet as WalletIcon } from 'lucide-react'
import { api } from '@/services/api'
import { Alert, Card, EmptyState, ErrorState, Loading } from '@/components/ui'

export default function Wallets() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['wallets'],
    queryFn: api.listWallets,
    refetchInterval: 30000,
  })

  if (isLoading) return <Loading label="Loading wallets" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />

  const wallets = data?.wallets ?? []

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-slate-100">Wallet Manager</h1>
        <p className="mt-1 text-sm text-slate-400">Configured accounts and their networks</p>
      </header>

      <Alert kind="info" title="Secrets are never displayed">
        Private keys and seed phrases are encrypted with AES-256-GCM in the backend vault and are
        never sent to the browser, the LLM, or the logs. This view shows public addresses only.
      </Alert>

      {wallets.length === 0 ? (
        <EmptyState
          title="No wallets configured"
          description="Import wallets through the backend API to enable campaign participation."
          icon={<WalletIcon className="h-8 w-8" />}
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {wallets.map((wallet) => (
            <Card key={wallet.id}>
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="truncate font-medium text-slate-100">{wallet.label}</h3>
                  <p className="font-mono text-xs text-slate-500">{wallet.chain_type}</p>
                </div>
                <span
                  className={`badge shrink-0 ${
                    wallet.is_active ? 'bg-green-900 text-green-300' : 'bg-slate-800 text-slate-400'
                  }`}
                >
                  {wallet.is_active ? 'active' : 'inactive'}
                </span>
              </div>

              <p className="mt-3 break-all rounded bg-surface-200 px-2 py-1.5 font-mono text-xs text-slate-400">
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
            </Card>
          ))}
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
