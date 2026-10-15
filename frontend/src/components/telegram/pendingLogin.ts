export type LoginStep = 'code' | 'password'

/**
 * What survives leaving the browser for the Telegram app (mobile browsers often reload the page
 * while it is in the background): only the pending login's id, label and step. The phone, the
 * login code and the 2FA password are never stored. sessionStorage is per tab and cleared when
 * the tab closes; the id is useless without the signed-in session and expires with the server's
 * 10-minute window.
 */
export interface PendingLogin {
  loginId: string
  label: string
  step: LoginStep
  expiresAt: number
}
const PENDING_KEY = 'airdropos.telegram.pendingLogin'

export function loadPendingLogin(): PendingLogin | null {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY)
    if (!raw) return null
    const v = JSON.parse(raw) as PendingLogin
    if (!v.loginId || typeof v.expiresAt !== 'number' || v.expiresAt <= Date.now()) {
      sessionStorage.removeItem(PENDING_KEY)
      return null
    }
    return v
  } catch {
    return null
  }
}
export function savePendingLogin(v: PendingLogin | null) {
  try {
    if (v) sessionStorage.setItem(PENDING_KEY, JSON.stringify(v))
    else sessionStorage.removeItem(PENDING_KEY)
  } catch {
    // storage unavailable (private mode): the flow still works until the page reloads
  }
}

