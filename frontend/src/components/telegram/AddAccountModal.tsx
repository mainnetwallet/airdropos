import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import Modal from '@/components/live/Modal'
import { api } from '@/services/api'
import { savePendingLogin, type PendingLogin, type LoginStep } from './pendingLogin'

type Step = 'details' | LoginStep

/**
 * Add a Telegram account: label + phone -> login code -> (2FA password).
 * The phone, code and password live only in this component's state and are cleared as soon as
 * they are sent. They are never stored in the browser (no localStorage, no query cache).
 */
export default function AddAccountModal({
  hasAccounts,
  resume,
  onClose,
}: {
  hasAccounts: boolean
  resume: PendingLogin | null
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [step, setStep] = useState<Step>(resume?.step ?? 'details')
  const [label, setLabel] = useState(resume?.label ?? '')
  const [phone, setPhone] = useState('')
  const [makeDefault, setMakeDefault] = useState(false)
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const loginId = useRef<string | null>(resume?.loginId ?? null)
  const expiresAt = useRef<number>(resume?.expiresAt ?? 0)

  const remember = (next: Step) => {
    if (loginId.current && next !== 'details') {
      savePendingLogin({ loginId: loginId.current, label: label.trim(), step: next, expiresAt: expiresAt.current })
    }
  }
  const forget = () => {
    loginId.current = null
    savePendingLogin(null)
  }

  // Closing (backdrop tap, Escape, X) only hides the dialog: a stray tap when coming back from the
  // Telegram app must not throw the pending login away. "Cancel login" below does that on purpose.
  const close = () => onClose()
  const cancelLogin = () => {
    if (loginId.current) void api.telegramLoginCancel(loginId.current).catch(() => undefined)
    forget()
    onClose()
  }
  useEffect(() => () => setPassword(''), [])

  const finish = () => {
    forget()
    void qc.invalidateQueries({ queryKey: ['telegram-accounts'] })
    void qc.invalidateQueries({ queryKey: ['worker-status-mini'] })
    onClose()
  }

  const start = useMutation({
    mutationFn: () => api.telegramLoginStart({ label: label.trim(), phone: phone.trim(), make_default: makeDefault }),
    onSuccess: (r) => {
      loginId.current = r.login_id
      expiresAt.current = Date.now() + Math.max(30, r.expires_in - 5) * 1000
      setPhone('')
      setStep('code')
      remember('code')
    },
  })
  const sendCode = useMutation({
    mutationFn: () => api.telegramLoginCode(loginId.current ?? '', code.trim()),
    onSuccess: (r) => {
      setCode('')
      if (r.stage === 'password') {
        setStep('password')
        remember('password')
      } else finish()
    },
    onError: (e: Error & { status?: number }) => {
      // A wrong code can be retried; anything else ends this login on the server.
      if (e.status === 410 || e.status === 429 || e.status === 409) {
        forget()
        setCode('')
        setStep('details')
      }
    },
  })
  const sendPassword = useMutation({
    mutationFn: () => api.telegramLoginPassword(loginId.current ?? '', password),
    onSuccess: () => {
      setPassword('')
      finish()
    },
    onError: (e: Error & { status?: number }) => {
      if (e.status === 410 || e.status === 429 || e.status === 409) {
        forget()
        setPassword('')
        setStep('details')
      }
    },
  })

  const busy = start.isPending || sendCode.isPending || sendPassword.isPending
  const error = (start.error ?? sendCode.error ?? sendPassword.error) as Error | null

  return (
    <Modal title="Add Telegram account" onClose={close}>
      <div className="space-y-4">
        {step === 'details' && (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault()
              sendCode.reset()
              sendPassword.reset()
              start.mutate()
            }}
          >
            <label className="block text-xs font-medium text-slate-400">
              Label
              <input
                className="input mt-1 w-full"
                placeholder="e.g. main, farm-2"
                maxLength={40}
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                autoFocus
              />
            </label>
            <label className="block text-xs font-medium text-slate-400">
              Phone number
              <input
                className="input mt-1 w-full"
                placeholder="+8801XXXXXXXXX"
                inputMode="tel"
                autoComplete="off"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
              />
            </label>
            {hasAccounts && (
              <label className="flex items-center gap-2 text-xs text-slate-300">
                <input type="checkbox" checked={makeDefault} onChange={(e) => setMakeDefault(e.target.checked)} />
                Make this the default account
              </label>
            )}
            <p className="text-xs text-slate-500">
              Telegram will send a login code to this account. The phone number is used once and never saved; only the
              profile name and the login session are kept on this server.
            </p>
            {error && <p className="text-sm text-status-error">{error.message}</p>}
            <button type="submit" className="btn btn-primary w-full" disabled={busy || !label.trim() || !phone.trim()}>
              {start.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Send login code
            </button>
          </form>
        )}

        {step === 'code' && (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault()
              sendCode.mutate()
            }}
          >
            <p className="text-sm text-slate-300">
              Enter the code Telegram sent to <span className="font-semibold">{label.trim()}</span> (check the Telegram app
              on that account). You can switch to Telegram and come back; this login waits for you.
            </p>
            <input
              className="input w-full text-center text-lg tracking-widest"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="12345"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoFocus
            />
            {error && <p className="text-sm text-status-error">{error.message}</p>}
            <button type="submit" className="btn btn-primary w-full" disabled={busy || !code.trim()}>
              {sendCode.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Log in
            </button>
            <button type="button" className="w-full text-center text-xs text-slate-500 underline" onClick={cancelLogin}>
              Cancel this login
            </button>
          </form>
        )}

        {step === 'password' && (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault()
              sendPassword.mutate()
            }}
          >
            <p className="text-sm text-slate-300">This account has two-step verification. Enter its password.</p>
            <input
              className="input w-full"
              type="password"
              autoComplete="off"
              placeholder="2FA password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
            />
            {error && <p className="text-sm text-status-error">{error.message}</p>}
            <button type="submit" className="btn btn-primary w-full" disabled={busy || !password}>
              {sendPassword.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Log in
            </button>
            <button type="button" className="w-full text-center text-xs text-slate-500 underline" onClick={cancelLogin}>
              Cancel this login
            </button>
          </form>
        )}
      </div>
    </Modal>
  )
}
