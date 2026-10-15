import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import Modal from '@/components/live/Modal'
import { api } from '@/services/api'
import type { TelegramAccount } from '@/types'

export default function RemoveAccountModal({ account, onClose }: { account: TelegramAccount; onClose: () => void }) {
  const qc = useQueryClient()
  const [revoke, setRevoke] = useState(true)
  const remove = useMutation({
    mutationFn: () => api.telegramRemoveAccount(account.label, revoke),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['telegram-accounts'] })
      void qc.invalidateQueries({ queryKey: ['worker-status-mini'] })
      onClose()
    },
  })
  return (
    <Modal title="Remove account" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-slate-300">
          Remove <span className="font-semibold">{account.profile_name ?? account.label}</span> ({account.label})? The agent
          will no longer be able to use it, and its saved login is deleted from this server.
          {account.is_default && ' Another account becomes the default.'}
        </p>
        <label className="flex items-start gap-2 text-xs text-slate-300">
          <input type="checkbox" className="mt-0.5" checked={revoke} onChange={(e) => setRevoke(e.target.checked)} />
          <span>Also log this session out on Telegram (it disappears from the account&apos;s Devices list).</span>
        </label>
        {remove.error && <p className="text-sm text-status-error">{(remove.error as Error).message}</p>}
        <div className="flex gap-2">
          <button type="button" className="btn btn-ghost flex-1" onClick={onClose} disabled={remove.isPending}>
            Cancel
          </button>
          <button type="button" className="btn btn-danger flex-1" onClick={() => remove.mutate()} disabled={remove.isPending}>
            {remove.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Remove
          </button>
        </div>
      </div>
    </Modal>
  )
}
