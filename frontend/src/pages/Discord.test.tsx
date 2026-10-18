/**
 * Frontend behaviour tests for the Discord page.
 *
 * These do not re-test the backend. They pin the properties that can only be broken
 * on the client, and each one exists because getting it wrong would mislead the
 * operator rather than merely fail:
 *
 *  1. The profile selector is real, and a task is created against the profile that
 *     was selected - the profile IS the account, so sending it wrong is unrecoverable.
 *  2. A login state is never flattened into "signed out". `UNKNOWN` renders as "Not
 *     verified", because telling someone to sign in to an account that is already
 *     signed in is worse than saying nothing was determined.
 *  3. An UNCONFIRMED send is shown as unconfirmed, never as a success to retry.
 *  4. No browser session material is rendered, even if the backend handed some over.
 *
 * Everything is mocked at the API boundary. No Chrome, no discord.com, no login.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import type { DiscordOverview, DiscordProfile, DiscordTask } from '@/types/discord'

const discordStatus = vi.fn()
const discordTasks = vi.fn()
const discordActivity = vi.fn()
const openDiscordProfile = vi.fn()
const discordLoginState = vi.fn()
const createDiscordTask = vi.fn()
const discordTask = vi.fn()
const cancelTask = vi.fn()
const retryTask = vi.fn()

vi.mock('@/services/api', () => ({
  api: {
    discordStatus: () => discordStatus(),
    discordTasks: () => discordTasks(),
    discordActivity: () => discordActivity(),
    openDiscordProfile: (...args: unknown[]) => openDiscordProfile(...args),
    discordLoginState: (...args: unknown[]) => discordLoginState(...args),
    createDiscordTask: (...args: unknown[]) => createDiscordTask(...args),
    discordTask: (...args: unknown[]) => discordTask(...args),
    cancelTask: (...args: unknown[]) => cancelTask(...args),
    retryTask: (...args: unknown[]) => retryTask(...args),
  },
}))

import Discord from '@/pages/Discord'

// ------------------------------------------------------------ fixtures
function profile(overrides: Partial<DiscordProfile> = {}): DiscordProfile {
  return {
    id: 'discord-account-1',
    name: 'discord-account-1',
    description: '',
    launch_mode: 'managed',
    browser_type: 'chrome',
    headless: true,
    user_data_dir: 'C:/data/profiles/one',
    start_url: '',
    is_default_agent: true,
    last_used_at: '',
    last_opened_at: '',
    last_error: '',
    can_open: true,
    open_problems: [],
    ...overrides,
  }
}

const overview: DiscordOverview = {
  mode: 'browser',
  note: 'Browser-only. No bot token, no stored credential.',
  profiles: [profile(), profile({ id: 'discord-account-2', name: 'discord-account-2' })],
  profile_count: 2,
  openable_profile_count: 2,
  selected_profile_id: 'discord-account-1',
  catalog: {
    intents: ['read', 'draft', 'write', 'unknown'],
    operations: [
      { name: 'read_channel', label: 'Read the messages in a channel', intent: 'read', write: false, consequential: false, note: '' },
      { name: 'send_message', label: 'Send a message to a channel', intent: 'write', write: true, consequential: false, note: '' },
    ],
    write_operations: ['send_message'],
    consequential_operations: ['leave_server'],
    draft_behaviour: 'Drafting never opens the composer.',
  },
  policy_note: 'A read-only task can never send.',
  anti_spam_note: 'No bulk unsolicited messaging.',
  recent_activity: [],
  counts: { recent_total: 0, recent_success: 0, recent_failed: 0, recent_uncertain: 1, recent_needs_user: 0 },
  duplicate_window_hours: 6,
}

function discordTaskFixture(overrides: Partial<DiscordTask> = {}): DiscordTask {
  return {
    task_id: 'AIR-2026-10-18-000001',
    title: 'Read the announcements',
    status: 'NEEDS_USER_ACTION',
    progress: 0.5,
    retry_count: 0,
    max_retries: 3,
    next_planned_action: '',
    last_blocker: 'Discord requires login',
    created_at: '2026-10-18T10:00:00Z',
    updated_at: '2026-10-18T10:05:00Z',
    deadline_at: '',
    browser_profile_id: 'discord-account-1',
    intent: 'read',
    resume: {
      browser_profile_id: 'discord-account-1',
      operation: '',
      intent: 'read',
      channel: {},
      completed_steps: ['open_app'],
      pending_action: '',
      unconfirmed_sends: [],
      last_evidence: '',
      login_intervention_id: '',
      deadline: '',
      last_page_state: {},
    },
    checkpoint: { name: 'awaiting_operator:discord_login_required', saved_at: '2026-10-18T10:05:00Z' },
    login_intervention: {
      request_id: 'IR-ABC123',
      status: 'PENDING',
      required_action: 'Log in to Discord in the browser profile this task uses, then confirm.',
      problem_summary: 'Discord requires you to log in before this task can continue.',
    },
    ...overrides,
  }
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
  return render(<Discord />, { wrapper })
}

/**
 * Click a section tab.
 *
 * `SegmentedTabs` renders its tabs twice - a mobile bar and a desktop bar - with
 * `role="tab"` on each, so a single-role query would match two elements. Both are
 * wired to the same state change, so clicking either is equivalent.
 */
async function openTab(name: RegExp) {
  const tabs = await screen.findAllByRole('tab', { name })
  fireEvent.click(tabs[0])
}

beforeEach(() => {
  vi.clearAllMocks()
  discordStatus.mockResolvedValue(overview)
  discordTasks.mockResolvedValue({ tasks: [], count: 0 })
  discordActivity.mockResolvedValue({ activity: [], filters: {}, note: '' })
  discordLoginState.mockResolvedValue({
    ok: true,
    browser_profile_id: 'discord-account-1',
    url: 'https://discord.com/app',
    login_state: { state: 'LOGGED_IN', indicator: 'app_chrome', detail: '', observed_at: '2026-10-18T10:00:00Z' },
    verified: true,
  })
  createDiscordTask.mockResolvedValue({
    task_id: 'AIR-2026-10-18-000009',
    browser_profile_id: 'discord-account-1',
    intent: 'read',
    detected_signals: ['read'],
    status: 'READY',
    title: 'Discord: Read',
  })
})

afterEach(cleanup)

// ------------------------------------------------------------ tests
describe('Discord page: browser account', () => {
  it('lists the existing browser profiles and does not invent a new one', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/Browser Account/i)).toBeTruthy())
    // Only the two profiles that already exist, and a way out to manage them.
    expect(screen.getAllByText(/discord-account-1/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/discord-account-2/).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('link', { name: /Manage profiles/i }).length).toBeGreaterThan(0)
  })

  it('does not check the login state until the operator asks', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/Not checked yet/i)).toBeTruthy())
    expect(discordLoginState).not.toHaveBeenCalled()
  })

  it('reports a signed-in session only after an explicit check', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByRole('button', { name: /Check login status/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Check login status/i }))
    await waitFor(() => expect(screen.getByText('Signed in')).toBeTruthy())
    expect(discordLoginState).toHaveBeenCalledWith('discord-account-1')
  })

  it('renders an unknown login state as "Not verified", never as logged out', async () => {
    discordLoginState.mockResolvedValue({
      ok: false,
      browser_profile_id: 'discord-account-1',
      login_state: {
        state: 'UNKNOWN',
        indicator: 'discord_error_page',
        detail: 'Discord is showing an error page.',
        observed_at: '2026-10-18T10:00:00Z',
      },
      verified: false,
    })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /Check login status/i }))
    await waitFor(() => expect(screen.getByText('Not verified')).toBeTruthy())
    expect(screen.queryByText('Login required')).toBeNull()
    expect(screen.queryByText('Signed in')).toBeNull()
    // And it says why, so "not verified" is not mistaken for "fine".
    expect(screen.getByText(/error page/i)).toBeTruthy()
  })

  it('shows a missing browser as "Browser not running", not as a logout', async () => {
    discordLoginState.mockResolvedValue({
      ok: false,
      browser_profile_id: 'discord-account-1',
      login_state: { state: 'NO_BROWSER', indicator: '', detail: 'No browser is open.', observed_at: '' },
      verified: false,
    })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /Check login status/i }))
    await waitFor(() => expect(screen.getByText('Browser not running')).toBeTruthy())
    expect(screen.queryByText('Signed in')).toBeNull()
  })

  it('shows a genuine login requirement as "Login required"', async () => {
    discordLoginState.mockResolvedValue({
      ok: true,
      browser_profile_id: 'discord-account-1',
      login_state: {
        state: 'LOGGED_OUT',
        indicator: 'login_form',
        detail: 'Discord redirected this profile to https://discord.com/login.',
        observed_at: '2026-10-18T10:00:00Z',
      },
      verified: true,
    })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /Check login status/i }))
    await waitFor(() => expect(screen.getByText('Login required')).toBeTruthy())
    expect(screen.getByText(/will not type your password/i)).toBeTruthy()
  })

  it('opens the exact profile that was selected', async () => {
    openDiscordProfile.mockResolvedValue({
      profile_id: 'discord-account-2',
      profile_name: 'discord-account-2',
      url: 'https://discord.com/app',
      running: true,
      reused: false,
      mode: 'persistent',
      note: 'Discord is open in this profile.',
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Browser Account/i)).toBeTruthy())

    const select = screen.getAllByRole('combobox')[0] as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'discord-account-2' } })
    fireEvent.click(screen.getByRole('button', { name: /Open Discord in this profile/i }))

    await waitFor(() => expect(openDiscordProfile).toHaveBeenCalled())
    expect(openDiscordProfile.mock.calls[0][0]).toBe('discord-account-2')
  })
})

describe('Discord page: task composer', () => {
  it('sends the selected profile id with the new task', async () => {
    renderPage()
    await openTab(/New Task/i)

    const select = screen.getAllByRole('combobox')[0] as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'discord-account-2' } })

    const textarea = screen.getByPlaceholderText(/Read the latest announcements/i)
    fireEvent.change(textarea, { target: { value: 'Summarise the announcements in #announcements' } })
    fireEvent.click(screen.getByRole('button', { name: /Start task/i }))

    await waitFor(() => expect(createDiscordTask).toHaveBeenCalled())
    const body = createDiscordTask.mock.calls[0][0]
    expect(body.browser_profile_id).toBe('discord-account-2')
    expect(body.description).toContain('announcements')
  })

  it('offers a read-only intent that cannot send', async () => {
    renderPage()
    await openTab(/New Task/i)
    await waitFor(() => expect(screen.getByText(/Read only/i)).toBeTruthy())
    expect(screen.getByText(/Cannot send anything/i)).toBeTruthy()
    expect(screen.getByText(/never opens the composer/i)).toBeTruthy()
  })
})

describe('Discord page: activity and login recovery', () => {
  it('never presents an unconfirmed send as a success', async () => {
    discordActivity.mockResolvedValue({
      activity: [
        {
          id: 'a1',
          browser_profile_id: 'discord-account-1',
          operation: 'send_message',
          intent: 'write',
          target: 'text:abc in #announcements',
          status: 'FAILED',
          operation_key: 'send:discord-account-1:channel:2:text:abc',
          outcome_uncertain: true,
          channel: { channel_id: '2', channel_name: 'announcements', label: '#announcements' },
          message_id: '',
          permalink: '',
          excerpt: '',
          evidence_path: '',
          error: 'The message was submitted but Discord did not confirm it arrived.',
          error_category: 'DISCORD_OUTCOME_UNKNOWN',
          duration_ms: 1200,
          task_id: 'AIR-2026-10-18-000001',
          created_at: '2026-10-18T10:05:00Z',
        },
      ],
      filters: {},
      note: '',
    })
    renderPage()
    await openTab(/Activity/i)

    await waitFor(() => expect(screen.getByText('UNCONFIRMED')).toBeTruthy())
    expect(screen.getByText(/never repeats such a message automatically/i)).toBeTruthy()
    expect(screen.queryByText('SUCCESS')).toBeNull()
  })

  it('shows a pending Discord login request with an action to open the profile', async () => {
    discordTasks.mockResolvedValue({ tasks: [discordTaskFixture()], count: 1 })
    renderPage()
    await openTab(/Login Recovery/i)

    await waitFor(() => expect(screen.getByText('Discord login required')).toBeTruthy())
    expect(screen.getByText(/AIR-2026-10-18-000001/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Open Discord browser/i })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Open Discord browser/i }))
    await waitFor(() => expect(openDiscordProfile).toHaveBeenCalledWith('discord-account-1'))
  })

  it('says plainly that no credential is stored with the request', async () => {
    discordTasks.mockResolvedValue({ tasks: [discordTaskFixture()], count: 1 })
    renderPage()
    await openTab(/Login Recovery/i)
    await waitFor(() =>
      expect(screen.getByText(/No password, cookie or token is stored/i)).toBeTruthy(),
    )
  })
})

describe('Discord page: session data never reaches the DOM', () => {
  it('renders no cookie, token or authorization material even if handed some', async () => {
    // A deliberately hostile backend response. The page must not surface any of it.
    discordStatus.mockResolvedValue({
      ...overview,
      profiles: [
        {
          ...profile(),
          description: 'cookie=SESSION_SECRET; authorization=Bearer abc123',
        },
      ],
    })
    const { container } = renderPage()
    await waitFor(() => expect(screen.getByText(/Browser Account/i)).toBeTruthy())
    const html = container.innerHTML
    expect(html).not.toMatch(/SESSION_SECRET/)
    expect(html).not.toMatch(/Bearer\s/)
    expect(html).not.toMatch(/cookie=/i)
  })
})