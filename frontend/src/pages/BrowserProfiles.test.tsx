/**
 * Browser Profiles page: it must render exactly what the backend reported, never an
 * optimistic "running". Every status in these tests comes from the stubbed API response.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import BrowserProfiles from './BrowserProfiles'
import type { BrowserProfileInfo, BrowserProfilesResponse } from '@/types/live'

const profile = (over: Partial<BrowserProfileInfo>): BrowserProfileInfo => ({
  id: 'p-1',
  name: 'Trading',
  description: 'main profile',
  viewport_width: 1280,
  viewport_height: 800,
  locale: '',
  timezone_id: '',
  start_url: '',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  has_saved_state: false,
  browser_type: 'chrome',
  launch_mode: 'managed',
  executable_path: '',
  resolved_executable: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  user_data_dir: '',
  resolved_user_data_dir: 'C:\\Users\\op\\AppData\\Local\\AirdropOS\\ChromeProfiles\\p-1',
  headless: false,
  debugger_host: '',
  debugger_port: 0,
  unpacked_extensions: '',
  is_default_agent: false,
  last_used_at: '',
  last_opened_at: '',
  last_error: '',
  last_recovery: '',
  configuration_problems: [],
  session_status: null,
  browser_running: false,
  connection: 'not_running',
  tabs: [],
  tab_count: 0,
  active_url: '',
  active_title: '',
  extensions: [],
  extension_count: 0,
  limitations: [],
  ...over,
})

function payload(over: Partial<BrowserProfilesResponse> = {}): BrowserProfilesResponse {
  return {
    profiles: [],
    default_agent_profile: null,
    worker_reachable: true,
    chrome: { found: true, path: 'C:\\chrome.exe', source: 'auto-detected', version: '155.0.0.0', detail: '' },
    profile_root: 'C:\\Users\\op\\AppData\\Local\\AirdropOS\\ChromeProfiles',
    persistent_profiles_enabled: true,
    ...over,
  }
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <BrowserProfiles />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return client
}

let response: BrowserProfilesResponse
let requests: Array<{ url: string; method: string; body?: string }>

beforeEach(() => {
  response = payload()
  requests = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      requests.push({ url, method, body: init?.body as string | undefined })
      if (method === 'POST' && /\/open$/.test(url)) {
        response = payload({
          ...response,
          profiles: response.profiles.map((p) => ({ ...p, browser_running: true, connection: 'managed_persistent' })),
        })
        return new Response(JSON.stringify({ profile: response.profiles[0], reused: false }), { status: 200 })
      }
      if (method === 'POST' && /\/close$/.test(url)) {
        response = payload({
          ...response,
          profiles: response.profiles.map((p) => ({ ...p, browser_running: false, connection: 'not_running' })),
        })
        return new Response(JSON.stringify({ closed: 'p-1', was_running: true, profile_data_kept: true }), { status: 200 })
      }
      return new Response(JSON.stringify(response), { status: 200 })
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Browser Profiles page', () => {
  it('reports that Google Chrome was not found instead of pretending it can launch', async () => {
    response = payload({
      chrome: { found: false, path: '', source: '', version: '', detail: 'Set BROWSER_CHROME_EXECUTABLE.' },
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Google Chrome was not found/i)).toBeTruthy())
  })

  it('shows the real chrome profile directory and executable the backend resolved', async () => {
    response = payload({ profiles: [profile({})] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Trading')).toBeTruthy())
    expect(screen.getByText(/AirdropOS\\ChromeProfiles\\p-1/)).toBeTruthy()
    // The card shows the per-profile executable; the banner shows the detected one.
    expect(screen.getAllByText(/chrome\.exe/i).length).toBeGreaterThan(0)
    expect(screen.getByText('Managed persistent Chrome')).toBeTruthy()
  })

  it('shows a stopped profile as stopped, never as running', async () => {
    response = payload({ profiles: [profile({})] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Stopped')).toBeTruthy())
    expect(screen.queryByText('Running')).toBeNull()
    expect(screen.getByRole('button', { name: /open browser/i })).toBeTruthy()
  })

  it('says "status unknown" when the worker cannot be reached, not "stopped"', async () => {
    response = payload({
      worker_reachable: false,
      profiles: [profile({ session_status: 'unknown', browser_running: false })],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Status unknown')).toBeTruthy())
    expect(screen.queryByText('Stopped')).toBeNull()
    expect(screen.getByText(/worker is unreachable/i)).toBeTruthy()
  })

  it('shows the running state the backend reported, with its tabs and current page', async () => {
    response = payload({
      profiles: [
        profile({
          browser_running: true,
          connection: 'managed_persistent',
          session_status: 'running',
          tab_count: 2,
          active_title: 'Example Domain',
          extension_count: 3,
        }),
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Running')).toBeTruthy())
    expect(screen.getByText(/Example Domain/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /stop browser/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /open browser/i })).toBeNull()
  })

  it('marks the agent default profile from the backend, never from local state', async () => {
    response = payload({ profiles: [profile({ is_default_agent: true })], default_agent_profile: 'p-1' })
    renderPage()
    await waitFor(() => expect(screen.getByText('Agent default')).toBeTruthy())
    expect(screen.getByRole('button', { name: /unset default/i })).toBeTruthy()
  })

  it('surfaces configuration problems and the last error instead of hiding them', async () => {
    response = payload({
      profiles: [
        profile({ configuration_problems: ['Chrome executable does not exist: /nope/chrome.exe'], last_error: 'Chrome refused to start' }),
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Chrome executable does not exist/i)).toBeTruthy())
    expect(screen.getByText(/Chrome refused to start/)).toBeTruthy()
  })

  it('offers the two browser modes and their real configuration in the create form', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /new profile/i }))
    await waitFor(() => expect(screen.getByText(/Mode A — Managed persistent Chrome/i)).toBeTruthy())
    expect(screen.getByText(/Mode B — Connect to an existing Chrome/i)).toBeTruthy()
    expect(screen.getByLabelText(/Chrome profile directory/i)).toBeTruthy()
    expect(screen.getByText(/AirdropOS\\ChromeProfiles/)).toBeTruthy()
  })

  it('switching to Mode B asks for a loopback DevTools port, not a profile directory', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /new profile/i }))
    fireEvent.click(await screen.findByText(/Mode B — Connect to an existing Chrome/i))
    await waitFor(() => expect(screen.getByLabelText(/DevTools port/i)).toBeTruthy())
    expect(screen.getByText(/remote-debugging-port/i)).toBeTruthy()
  })

  it('requires the profile id before it will delete Chrome profile data', async () => {
    response = payload({ profiles: [profile({})] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /delete data/i }))
    const dialog = await screen.findByText(/permanently deletes/i)
    expect(dialog).toBeTruthy()
    const confirm = screen.getByRole('button', { name: /permanently delete/i })
    expect((confirm as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/type the profile id/i), { target: { value: 'p-1' } })
    await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(false))
  })

  it('never enables "Delete data" for a profile AirdropOS does not own', async () => {
    response = payload({
      profiles: [profile({ launch_mode: 'attach', debugger_host: '127.0.0.1', debugger_port: 9222 })],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Connect to existing Chrome')).toBeTruthy())
    const wipe = screen.getByRole('button', { name: /delete data/i }) as HTMLButtonElement
    expect(wipe.disabled).toBe(true)
    expect(screen.getByRole('button', { name: /set as agent default/i }).hasAttribute('disabled')).toBe(true)
  })

  it('tells the operator that deleting a configuration keeps the Chrome data', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    response = payload({ profiles: [profile({})] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /delete config/i }))
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/Chrome profile data .* kept on disk/i))
    await waitFor(() =>
      expect(requests.some((r) => r.method === 'DELETE' && r.url.includes('/browser/profiles/p-1'))).toBe(true),
    )
  })

  it('open browser posts to the backend and then shows what came back', async () => {
    response = payload({ profiles: [profile({})] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /open browser/i }))
    await waitFor(() =>
      expect(requests.some((r) => r.method === 'POST' && r.url.includes('/browser/profiles/p-1/open'))).toBe(true),
    )
    await waitFor(() => expect(screen.getByText(/Chrome started for "Trading"/i)).toBeTruthy())
  })

  it('shows the real error the backend returned instead of an optimistic success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        requests.push({ url, method })
        if (method === 'POST' && /\/open$/.test(url)) {
          return new Response(JSON.stringify({ detail: { error: 'Chrome refused to start' } }), { status: 409 })
        }
        return new Response(JSON.stringify(payload({ profiles: [profile({})] })), { status: 200 })
      }),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /open browser/i }))
    await waitFor(() => expect(screen.getByText(/Chrome refused to start/)).toBeTruthy())
    expect(screen.queryByText(/Chrome started/i)).toBeNull()
  })
})
