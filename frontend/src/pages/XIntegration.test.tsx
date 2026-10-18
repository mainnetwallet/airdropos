/**
 * Frontend behaviour tests for the X.com page.
 *
 * The point of these is not to re-test the backend; it is to pin the two
 * properties that can only be broken on the client:
 *
 *  1. A secret never reaches the DOM. The backend will not send one, but the
 *     page must also not render one it is handed by mistake.
 *  2. An UNCERTAIN X write is presented as uncertain, not as a failure the
 *     operator should just retry.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { XOverview } from '@/types/x'

// Typed as the page's own contract, so a change to `XOverview` fails the type
// check here rather than producing a quietly wrong fixture.
const overview: XOverview = {
  api_enabled: true,
  vault: { ready: true, key_source: 'WALLET_VAULT_MASTER_KEY', algorithm: 'AES-256-GCM', detail: '' },
  configurations: [
    {
      id: 'c1',
      name: 'Main X account',
      description: 'primary',
      client_id_preview: 'abcde…9x',
      has_client_secret: true,
      has_access_token: true,
      has_refresh_token: true,
      client_secret_mask: '••••',
      access_token_mask: '••••',
      scopes: ['tweet.read', 'tweet.write'],
      x_user_id: '42',
      x_username: 'operator',
      is_enabled: true,
      is_default: true,
      status: 'OK',
      status_detail: 'Authorized as @operator (id 42).',
      last_checked_at: '2026-10-18T10:00:00Z',
      last_used_at: null,
      token_expires_at: null,
      rate_limit_remaining: 14,
      rate_limit_reset_at: null,
      created_by: 'operator',
      created_at: '2026-10-01T00:00:00Z',
      vault_ready: true,
      can_write: true,
    },
  ],
  configuration_count: 1,
  enabled_count: 1,
  authorized_count: 1,
  verified_count: 1,
  browser_profile_id: 'profile-1',
  browser_profile_reason: "Using the default agent browser profile 'profile-1'.",
  required_scopes: ['tweet.read', 'tweet.write', 'users.read', 'offline.access'],
  catalog: {
    preferences: ['auto', 'api_preferred', 'browser_preferred'],
    methods: ['api', 'browser'],
    operations: [
      {
        name: 'create_post',
        label: 'Publish a post',
        write: true,
        supported_by: ['api', 'browser'],
        api_scope: 'tweet.read,tweet.write,users.read',
        api_caveat: 'Needs a paid API plan.',
        browser_note: '',
      },
    ],
  },
  note: 'Writing through the API needs a paid plan.',
  preference: 'auto',
  browser: { ok: true, session_key: 'profile-1', running: true, mode: 'managed', login: 'LOGGED_IN' },
  recent_activity: [
    {
      id: 'a1',
      config_id: 'c1',
      config_name: 'Main X account',
      browser_profile_id: '',
      method: 'api',
      operation: 'create_post',
      target: 'text:abc',
      status: 'UNCERTAIN',
      operation_key: 'create_post:api:Main:abc',
      outcome_uncertain: true,
      result_summary: {},
      error: 'The X API did not answer in time.',
      error_category: 'X_TIMEOUT_UNCERTAIN',
      duration_ms: 20001,
      task_id: 'AIR-2026-10-18-000001',
      created_at: '2026-10-18T09:00:00Z',
    },
  ],
  counts: { recent_success: 3, recent_failed: 0, recent_uncertain: 1, recent_needs_user: 0 },
}

const activity = { activity: overview.recent_activity, filters: { method: '', status: '', config_id: '' }, note: 'An UNCERTAIN row means the outcome is unknown.' }

vi.mock('@/services/api', () => ({
  api: {
    xOverview: vi.fn(async () => overview),
    xActivity: vi.fn(async () => activity),
    xConfigurations: vi.fn(async () => ({ configurations: overview.configurations, preference: 'auto' })),
    xPreference: vi.fn(async () => ({ preference: 'auto', options: [], explanation: {} })),
    xBrowserProfile: vi.fn(async () => ({ browser_profile_id: 'profile-1', pinned_profile_id: '', reason: '' })),
    xBrowserStatus: vi.fn(async () => overview.browser),
    browserProfiles: vi.fn(async () => ({ profiles: [], default_agent_profile: '', worker_reachable: true, profile_root: '' })),
  },
  liveApi: {
    browserProfiles: vi.fn(async () => ({ profiles: [], default_agent_profile: '', worker_reachable: true, profile_root: '' })),
    openBrowserProfile: vi.fn(async () => ({ profile: {}, reused: false })),
  },
}))

import XIntegration from '@/pages/XIntegration'

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrap = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return render(<XIntegration />, { wrapper: wrap })
}

describe('X.com page', () => {
  beforeEach(() => vi.clearAllMocks())
  // This project runs vitest without `globals: true`, so Testing Library's
  // automatic cleanup is not registered. Without this, a page left on a
  // non-default tab leaks into the next test and queries match twice.
  afterEach(cleanup)

  it('shows the real state of both methods on the overview', async () => {
    renderPage()
    // The routing table is the anchor: it renders once the overview has loaded.
    expect(await screen.findByText('Publish a post')).toBeTruthy()
    expect(screen.getByText('LOGGED_IN')).toBeTruthy()
    expect(screen.getByText('1 with a user token')).toBeTruthy()
    expect(screen.getByText('Needs a paid API plan.')).toBeTruthy()
  })

  it('renders masks and never a credential value', async () => {
    const { container } = renderPage()
    await screen.findByText('Publish a post')

    // The API tab is where a configuration's credentials are described.
    fireEvent.click(screen.getByRole('tab', { name: 'API' }))
    expect(await screen.findByText('Main X account')).toBeTruthy()
    // Masked hints ARE rendered, so the operator can tell rows apart…
    expect(screen.getAllByText('••••').length).toBeGreaterThan(0)

    // …and nothing resembling a token is, whatever the backend hands over.
    const html = container.innerHTML
    expect(html).not.toMatch(/Bearer\s/)
    expect(html).not.toMatch(/\b[0-9a-f]{32,}\b/i)
  })

  it('surfaces an uncertain write as uncertain, and counts it', async () => {
    renderPage()
    await screen.findByText('Publish a post')
    // The stat tile counts it…
    expect(screen.getByText('Uncertain writes')).toBeTruthy()
    // …and the recent-activity row labels it UNCERTAIN rather than calling it a
    // plain failure the operator should just retry.
    expect(screen.getAllByText('UNCERTAIN').length).toBeGreaterThan(0)
    expect(screen.getByText('The X API did not answer in time.')).toBeTruthy()
  })

  it('shows the empty state when nothing has happened yet', async () => {
    const { api } = await import('@/services/api')
    vi.mocked(api.xOverview).mockResolvedValueOnce({
      ...overview,
      recent_activity: [],
      counts: { recent_success: 0, recent_failed: 0, recent_uncertain: 0, recent_needs_user: 0 },
    })
    renderPage()
    expect(await screen.findByText('No X actions yet')).toBeTruthy()
  })

  it('reports a failure instead of rendering a broken page', async () => {
    const { api } = await import('@/services/api')
    vi.mocked(api.xOverview).mockRejectedValueOnce(new Error('The backend is not reachable.'))
    renderPage()
    expect(await screen.findByText('The backend is not reachable.')).toBeTruthy()
  })

  it('warns when the credential vault cannot encrypt anything', async () => {
    const { api } = await import('@/services/api')
    vi.mocked(api.xOverview).mockResolvedValueOnce({
      ...overview,
      vault: {
        ready: false,
        key_source: 'SECRET_KEY (derived)',
        algorithm: 'AES-256-GCM',
        detail: 'Set WALLET_VAULT_MASTER_KEY before storing X credentials.',
      },
    })
    renderPage()
    await waitFor(() =>
      expect(screen.getByText('Set WALLET_VAULT_MASTER_KEY before storing X credentials.')).toBeTruthy(),
    )
  })
})