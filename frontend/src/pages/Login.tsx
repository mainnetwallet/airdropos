/** Operator login. The password is set with `python run.py --set-password`; there is no default. */

import { useState } from 'react'
import { LogIn, ShieldAlert } from 'lucide-react'
import { api } from '@/services/api'

export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [email, setEmail] = useState('operator@airdropos.local')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (busy || !password) return
    setBusy(true)
    setError('')
    try {
      await api.login(email.trim(), password)
      onSuccess()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-surface-50 p-6">
      <div className="card w-full max-w-sm space-y-4">
        <div className="flex items-center gap-2">
          <ShieldAlert className="h-7 w-7 text-accent" />
          <div>
            <p className="font-bold text-slate-100">AirdropOS</p>
            <p className="text-xs text-slate-500">Operator login</p>
          </div>
        </div>

        <div className="space-y-1">
          <label className="stat-label" htmlFor="email">Email</label>
          <input
            id="email"
            className="input"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
          />
        </div>

        <div className="space-y-1">
          <label className="stat-label" htmlFor="password">Password</label>
          <input
            id="password"
            className="input"
            type="password"
            autoComplete="current-password"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
          />
        </div>

        {error && <p className="text-sm text-status-error">{error}</p>}

        <button className="btn-primary w-full justify-center" onClick={submit} disabled={busy || !password}>
          <LogIn className="h-4 w-4" />
          {busy ? 'Signing in...' : 'Sign in'}
        </button>

        <p className="text-[11px] leading-snug text-slate-500">
          First time? Create the account once with <code>POST /api/v1/auth/bootstrap</code> and set the
          password with <code>python run.py --set-password</code>.
        </p>
      </div>
    </div>
  )
}
