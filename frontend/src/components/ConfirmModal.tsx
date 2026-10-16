import { useEffect, useRef, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import Modal from '@/components/live/Modal'

/**
 * Confirmation dialog that matches the rest of the app (replaces `window.confirm`).
 * Cancel gets the focus first, so an accidental Enter never destroys anything.
 */
export default function ConfirmModal({
  title,
  children,
  confirmLabel = 'Delete',
  danger = true,
  onConfirm,
  onClose,
}: {
  title: string
  children: ReactNode
  confirmLabel?: string
  danger?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  useEffect(() => cancelRef.current?.focus(), [])

  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-5">
        <div className="flex items-start gap-3">
          {danger && (
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-status-error/15 text-status-error">
              <AlertTriangle className="h-5 w-5" />
            </span>
          )}
          <div className="min-w-0 text-sm leading-6 text-slate-300">{children}</div>
        </div>
        <div className="flex gap-2">
          <button ref={cancelRef} type="button" className="btn btn-ghost flex-1 justify-center" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={`btn flex-1 justify-center ${danger ? 'btn-danger' : 'btn-primary'}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  )
}
