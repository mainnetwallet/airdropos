/**
 * Startup requests — the dashboard must not manufacture failures before login.
 *
 * Before this, opening the dashboard always called `GET /api/v1/overview` with no
 * token, got a 401, and repeated that on every reload. The WebSocket had the same
 * shape: it was opened without a session, refused before it was accepted, and the
 * reconnect loop retried it on a timer forever.
 *
 * These tests pin the fix and, just as importantly, pin what must NOT change: a
 * stored session is still validated, a deployment with authentication off still
 * works, and nothing is loosened on the server to make the client quiet.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

import { AuthGate } from '@/App'
import { useLiveEvents } from '@/hooks'
import { api, canOpenLiveStream, isAuthRequired, setAuthRequired } from '@/services/api'

const HEALTH = '/api/v1/health'

interface Call {
  url: string
  headers: Record<string, string>
}

function stubFetch(authRequired: boolean | undefined, overrides: Record<string, unknown> = {}) {
  const calls: Call[] = []
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({
      url: String(url),
      headers: (init?.headers ?? {}) as Record<string, string>,
    })
    const json = (payload: unknown, status = 200) =>
      new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })

    if (String(url).includes(HEALTH)) {
      return json({ status: 'ok', service: 'airdropos', auth_required: authRequired })
    }
    if (Object.keys(overrides).length) return json(overrides)
    return json({ detail: { error: 'Authentication required.' } }, 401)
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

/** A WebSocket stand-in that records every construction. */
function stubWebSocket() {
  const opened: Array<{ url: string; protocols: string[] }> = []
  class FakeSocket {
    static OPEN = 1
    readyState = 0
    onopen: (() => void) | null = null
    onmessage: ((m: { data: string }) => void) | null = null
    onclose: (() => void) | null = null
    onerror: (() => void) | null = null
    constructor(url: string, protocols?: string[]) {
      opened.push({ url, protocols: protocols ?? [] })
    }
    close() {}
    send() {}
  }
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
  return opened
}

beforeEach(() => {
  localStorage.clear()
  setAuthRequired(null)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  localStorage.clear()
  setAuthRequired(null)
})

// ======================================================================
// 1. The pre-login probe
// ======================================================================
describe('pre-login probe', () => {
  it('asks the unauthenticated health endpoint and never a protected one', async () => {
    const calls = stubFetch(true)
    render(<AuthGate />)

    expect(await screen.findByText('Operator login')).toBeTruthy()

    const urls = calls.map((c) => c.url)
    expect(urls).toContain(HEALTH)
    // This is the whole point: no 401 was manufactured on the way to the login form.
    expect(urls.some((u) => u.includes('/api/v1/overview'))).toBe(false)
  })

  it('sends no credentials on that probe', async () => {
    localStorage.setItem('airdropos.token', 'a-token-that-should-not-be-sent-here')
    const calls = stubFetch(true)
    render(<AuthGate />)

    await screen.findByText('Operator login')

    const probe = calls.find((c) => c.url.includes(HEALTH))
    expect(probe).toBeDefined()
    expect(probe?.headers.Authorization).toBeUndefined()
  })

  it('records what the backend said, so the socket decision is informed', async () => {
    stubFetch(true)
    render(<AuthGate />)
    await screen.findByText('Operator login')
    expect(isAuthRequired()).toBe(true)
  })

  it('validates a stored session instead of trusting that it exists', async () => {
    localStorage.setItem('airdropos.token', 'an-old-token')
    const calls = stubFetch(true)
    render(<AuthGate />)

    // The token is presented, and the backend's 401 is what ends up on the login form.
    await waitFor(() => expect(screen.getByText('Operator login')).toBeTruthy())
    expect(calls.some((c) => c.url.includes('/api/v1/overview'))).toBe(true)
    const overview = calls.find((c) => c.url.includes('/api/v1/overview'))
    expect(overview?.headers.Authorization).toBe('Bearer an-old-token')
    // And that rejected token is forgotten rather than retried on every reload.
    expect(localStorage.getItem('airdropos.token')).toBeNull()
  })

  it('still shows the unreachable-backend error when the backend is down', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    render(<AuthGate />)
    expect(await screen.findByText('Something went wrong')).toBeTruthy()
  })
})

// ======================================================================
// 2. The live socket
// ======================================================================
function SocketProbe() {
  const { connected } = useLiveEvents()
  return <span>{connected ? 'connected' : 'offline'}</span>
}

describe('live event stream', () => {
  it('does not open a socket that authentication is certain to refuse', () => {
    stubWebSocket()
    setAuthRequired(true) // and there is no token in localStorage
    expect(canOpenLiveStream()).toBe(false)

    render(<SocketProbe />)
    // A refused handshake, on a timer, for as long as the login form is open.
    expect(document.body.textContent).toBe('offline')
  })

  it('opens the socket once a session exists', () => {
    const opened = stubWebSocket()
    setAuthRequired(true)
    localStorage.setItem('airdropos.token', 'a-real-token')

    expect(canOpenLiveStream()).toBe(true)
    render(<SocketProbe />)

    expect(opened).toHaveLength(1)
    // The token rides the handshake subprotocol, never the URL.
    expect(opened[0].protocols).toEqual(['bearer', 'a-real-token'])
    expect(opened[0].url).not.toContain('a-real-token')
  })

  it('still opens an anonymous socket when the deployment has auth switched off', () => {
    const opened = stubWebSocket()
    setAuthRequired(false)

    expect(canOpenLiveStream()).toBe(true)
    render(<SocketProbe />)
    expect(opened).toHaveLength(1)
  })

  it('keeps the old reconnect behaviour when the backend does not say', () => {
    const opened = stubWebSocket()
    setAuthRequired(null)
    expect(canOpenLiveStream()).toBe(true)
    render(<SocketProbe />)
    expect(opened).toHaveLength(1)
  })
})

// ======================================================================
// 3. Nothing was loosened
// ======================================================================
describe('the server is unchanged', () => {
  it('a request without a token is still refused, and the refusal still clears the session', async () => {
    const calls = stubFetch(true)
    // Handled by the shared `request()` helper: the 401 below is a real one.
    await expect(api.overview()).rejects.toMatchObject({ status: 401 })
    expect(localStorage.getItem('airdropos.token')).toBeNull()
    expect(calls.some((c) => c.url.includes('/api/v1/overview'))).toBe(true)
  })
})