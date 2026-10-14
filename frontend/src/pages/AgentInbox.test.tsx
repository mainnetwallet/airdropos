/**
 * Agent Inbox — navigation, rendering, sending and live updates.
 *
 * The rule these tests enforce is the one the rest of the suite follows: the screen renders
 * what the BACKEND reported. There is no case here where a button appears without a real
 * call behind it, a task reports progress the API did not send, or a reply is shown as
 * successful when the server refused it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import AgentInbox from '@/pages/AgentInbox'
import { BOTTOM_TABS, NAV_SECTIONS, ROUTE_TABS } from '@/layouts/navigation'
import { reset as resetSeen, markSeen, isUnread } from '@/services/inboxSeen'
import type {
  InboxConversation,
  InboxConversationDetail,
  InboxListResponse,
} from '@/types'

// ----------------------------------------------------------------------
// Payload factories
// ----------------------------------------------------------------------
function conversation(over: Partial<InboxConversation> = {}): InboxConversation {
  return {
    conversation_id: 'c1',
    task_id: 'AIR-2026-000042',
    title: 'Join the announcement channel',
    task_status: 'NEEDS_USER_ACTION',
    task_progress: 0.5,
    status: 'OPEN',
    requires_reply: true,
    last_message_at: '2026-10-13T10:00:00+00:00',
    last_seq: 1,
    last_message_preview: 'Telegram session is unavailable for this task.',
    ...over,
  }
}

function list(over: Partial<InboxListResponse> = {}): InboxListResponse {
  return {
    conversations: [conversation()],
    count: 1,
    total: 1,
    needs_reply: 1,
    ...over,
  }
}

function detail(over: Partial<InboxConversationDetail> = {}): InboxConversationDetail {
  return {
    ...conversation(),
    messages: [
      {
        id: 'm1',
        role: 'assistant',
        kind: 'INTERVENTION_REQUEST',
        content: 'Telegram session is unavailable for this task.\n\nWhat you need to do: Restore it.',
        seq: 1,
        created_at: '2026-10-13T10:00:00+00:00',
        intervention_request_id: 'IR-1',
      },
    ],
    message_count: 1,
    has_more: false,
    open_request: {
      request_id: 'IR-1',
      task_id: 'AIR-2026-000042',
      conversation_id: 'c1',
      reason_code: 'TELEGRAM_SESSION_UNAVAILABLE',
      problem_summary: 'Telegram session is unavailable for this task.',
      detailed_explanation: null,
      required_action: 'Restore the Telegram session.',
      instructions: ['Re-run the interactive Telegram sign-in.'],
      integration_ref: 'telegram:agent',
      blocking_step: 'Step 1 (telegram_join_chat)',
      error_category: 'AUTH_FAILURE',
      status: 'PENDING',
      created_at: '2026-10-13T10:00:00+00:00',
      resolved_at: null,
      resolved_by: null,
      verification_method: null,
      verification_outcome: null,
      machine_verifiable: true,
      requires_explicit_authorization: false,
    },
    task: {
      task_id: 'AIR-2026-000042',
      title: 'Join the announcement channel',
      status: 'NEEDS_USER_ACTION',
      progress: 0.5,
      retry_count: 0,
      max_retries: 3,
      next_planned_action: 'Restore the Telegram session.',
      next_execution_at: null,
      last_updated: '2026-10-13T10:00:00+00:00',
      error_details: {},
      detail_url: '/tasks/AIR-2026-000042',
    },
    ...over,
  }
}

const TASK_REPORT = {
  task_id: 'AIR-2026-000042',
  title: 'Join the announcement channel',
  status: 'NEEDS_USER_ACTION',
  priority: 5,
  progress: 0.5,
  network: null,
  retry_count: 0,
  max_retries: 3,
  dependencies: [],
  planned_actions: [],
  pending_actions: [],
  completed_actions: [],
  failed_actions: [],
  checkpoints: 1,
  evidence_count: 0,
  error_details: {},
  next_planned_action: 'Restore the Telegram session.',
  blocker: 'Telegram session is unavailable',
  deadline_at: null,
  next_execution_at: null,
  last_updated: '2026-10-13T10:00:00+00:00',
  last_verification_at: null,
  recent_executions: [],
  recent_events: [{ type: 'STATUS_TRANSITION', severity: 'WARN', reason: 'parked', at: '2026-10-13T10:00:00+00:00' }],
  transactions: [],
}

// ----------------------------------------------------------------------
// Harness
// ----------------------------------------------------------------------
interface Routes {
  list?: InboxListResponse
  detail?: InboxConversationDetail
  task?: unknown
  sent?: unknown
  sendStatus?: number
  deleteStatus?: number
  deleteBody?: unknown
}

function stubFetch(routes: Routes) {
  const requests: Array<{ url: string; method: string; body: unknown }> = []
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    requests.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })

    const json = (payload: unknown, status = 200) =>
      new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })

    if (method === 'DELETE' && url.includes('/inbox/conversations/')) {
      return json(
        routes.deleteBody ?? {
          conversation_id: 'c1', task_id: 'AIR-2026-000042', deleted: true, messages: 1, intervention_requests: 1,
        },
        routes.deleteStatus ?? 200,
      )
    }
    if (url.includes('/inbox/conversations/') && url.includes('/messages')) {
      return json(routes.sent ?? {}, routes.sendStatus ?? 200)
    }
    if (url.includes('/inbox/conversations/')) return json(routes.detail ?? detail())
    if (url.includes('/inbox/conversations')) return json(routes.list ?? list())
    if (url.includes('/tasks/')) return json(routes.task ?? TASK_REPORT)
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
  return requests
}

function renderInbox(initialEntry = '/inbox') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/inbox" element={<AgentInbox />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return client
}

/**
 * The layout is responsive with CSS alone, so jsdom renders the task details twice:
 * once in the right column and once in the drawer shown below xl. Assertions about task
 * details are scoped to the right column.
 */
function detailsPanel() {
  return within(screen.getByRole('complementary', { name: 'Task details' }))
}

beforeEach(() => {
  resetSeen()
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ======================================================================
// 1. Navigation
// ======================================================================
describe('navigation', () => {
  it('lists Agent Inbox in the sidebar navigation', () => {
    const item = NAV_SECTIONS.flatMap((s) => s.items).find((i) => i.to === '/inbox')
    expect(item).toBeDefined()
    expect(item?.label).toBe('Agent Inbox')
  })

  it('shares the existing Chat bottom tab rather than adding a sixth', () => {
    const order = BOTTOM_TABS.map((t) => t.id)
    // The mobile order must stay Home -> Airdrops -> Chat -> Tasks -> Wallets.
    expect(order).toEqual(['home', 'airdrops', 'chat', 'tasks', 'wallets'])
    // And the Inbox highlights that tab rather than needing one of its own.
    expect(ROUTE_TABS.find(([prefix]) => prefix === '/inbox')?.[1]).toBe('chat')
  })
})

// ======================================================================
// 2. Conversation list
// ======================================================================
describe('conversation list', () => {
  it('loads and renders task conversations', async () => {
    stubFetch({})
    renderInbox()
    expect(await screen.findByText('Join the announcement channel')).toBeTruthy()
    expect(screen.getByText('AIR-2026-000042')).toBeTruthy()
    expect(screen.getByText('Telegram session is unavailable for this task.')).toBeTruthy()
  })

  it('groups a task needing a reply above everything else', async () => {
    stubFetch({
      list: list({
        needs_reply: 1,
        total: 2,
        count: 2,
        conversations: [
          conversation({ conversation_id: 'c-wait', task_id: 'AIR-2026-000002', title: 'Quiet task', requires_reply: false, task_status: 'COMPLETED' }),
          conversation(),
        ],
      }),
    })
    renderInbox()
    await screen.findByText('Join the announcement channel')
    const groups = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent ?? '')
    expect(groups[0]).toMatch(/Needs Your Reply/)
    // The first rendered row is the one needing a reply.
    const replyButton = screen.getByText('Reply')
    expect(replyButton.closest('button')?.textContent ?? '').toContain('Join the announcement channel')
  })

  it('shows an empty state rather than a fake conversation when there are none', async () => {
    stubFetch({ list: list({ conversations: [], count: 0, total: 0, needs_reply: 0 }) })
    renderInbox()
    expect(await screen.findByText('No task conversations yet')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /join the announcement/i })).toBeNull()
  })

  it('surfaces a load failure instead of pretending it is empty', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: 'backend down' }), { status: 500 })),
    )
    renderInbox()
    expect(await screen.findByText('Something went wrong')).toBeTruthy()
  })
})

// ======================================================================
// 3. Selecting a conversation
// ======================================================================
describe('conversation detail', () => {
  it('shows the messages for the selected conversation', async () => {
    stubFetch({})
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    expect(await screen.findByText(/Restore the Telegram session/)).toBeTruthy()
    expect(screen.getByText('Action needed')).toBeTruthy()
  })

  it('shows the real task state and links to the Task Manager', async () => {
    stubFetch({})
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    // A query parameter, because the Task Manager has no per-task route; it reads
    // ?task=<id> on load so the link actually opens that task.
    await waitFor(() =>
      expect(detailsPanel().getByRole('link', { name: /open in task manager/i })).toBeTruthy(),
    )
    expect(detailsPanel().getByRole('link', { name: /open in task manager/i }).getAttribute('href')).toBe(
      '/tasks?task=AIR-2026-000042',
    )
  })

  it('says so explicitly when the task record has no next step', async () => {
    stubFetch({
      task: { ...TASK_REPORT, next_planned_action: null, blocker: null, recent_events: [], recent_executions: [] },
    })
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    await waitFor(() =>
      expect(detailsPanel().getByText(/Unavailable — no next step recorded/)).toBeTruthy(),
    )
    expect(detailsPanel().getByText(/Unavailable — none reported/)).toBeTruthy()
    expect(detailsPanel().getByText(/Unavailable — no events recorded/)).toBeTruthy()
  })

  it('does not break when the conversation has no task attached', async () => {
    stubFetch({
      list: list({ conversations: [conversation({ task_id: null })], needs_reply: 1 }),
      detail: detail({ task_id: null, task: null }),
    })
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    expect(await screen.findByText(/not linked to a live task/i)).toBeTruthy()
    // No composer is offered, so nothing can be sent to a conversation with no task.
    expect(screen.queryByLabelText(/reply to the agent about this task/i)).toBeNull()
  })
})

// ======================================================================
// 4. Composing
// ======================================================================
describe('sending a reply', () => {
  async function openComposer() {
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    return screen.findByLabelText(/reply to the agent about this task/i)
  }

  it('posts the message with an idempotency token', async () => {
    const requests = stubFetch({
      sent: { status: 'RESUMED', resumed: true, task_id: 'AIR-2026-000042', verification: { method: 'test', ok: true, checkable: true, outcome: 'ok', detail: {} } },
    })
    const box = await openComposer()

    fireEvent.change(box, { target: { value: 'Telegram session is ready.' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => {
      const post = requests.find((r) => r.method === 'POST')
      expect(post).toBeDefined()
      expect(post?.body).toMatchObject({ message: 'Telegram session is ready.' })
      expect((post?.body as { client_token: string }).client_token).toBeTruthy()
    })
  })

  it('reports what the backend decided, including a real resumption', async () => {
    stubFetch({
      sent: {
        status: 'RESUMED',
        resumed: true,
        task_id: 'AIR-2026-000042',
        message_id: 'm2',
        agent_message_id: 'm3',
        request_id: 'IR-1',
        task_status: 'READY',
        verification: { method: 'telegram_is_user_authorized', ok: true, checkable: true, outcome: 'authorised', detail: {} },
      },
    })
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    fireEvent.change(await screen.findByLabelText(/reply to the agent about this task/i), {
      target: { value: 'Telegram session is ready.' },
    })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(await screen.findByText('Task resumed')).toBeTruthy()
    expect(screen.getByText(/telegram_is_user_authorized/)).toBeTruthy()
  })

  it('shows an honest "still blocked" result when verification fails', async () => {
    stubFetch({
      sent: {
        status: 'NEEDS_MORE_INFO',
        resumed: false,
        task_id: 'AIR-2026-000042',
        message_id: 'm2',
        agent_message_id: null,
        request_id: 'IR-1',
        task_status: 'NEEDS_USER_ACTION',
        verification: { method: 'telegram_is_user_authorized', ok: false, checkable: true, outcome: 'not authorised', detail: {} },
      },
    })
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    fireEvent.change(await screen.findByLabelText(/reply to the agent about this task/i), {
      target: { value: 'I think it is fixed' },
    })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(await screen.findByText('Still blocked')).toBeTruthy()
    expect(document.body.innerHTML).not.toMatch(/Task resumed/)
  })

  it('shows the send failure instead of pretending it worked', async () => {
    stubFetch({ sent: { detail: 'Conversation not found.' }, sendStatus: 404 })
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    fireEvent.change(await screen.findByLabelText(/reply to the agent about this task/i), {
      target: { value: 'hello' },
    })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent ?? '').toMatch(/conversation not found/i)
    expect(screen.queryByText('Task resumed')).toBeNull()
  })

  it('does not submit twice when clicked repeatedly', async () => {
    const requests = stubFetch({ sent: { status: 'RECORDED', resumed: false, task_id: 'AIR-2026-000042' } })
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    fireEvent.change(await screen.findByLabelText(/reply to the agent about this task/i), {
      target: { value: 'stop this task' },
    })
    const send = screen.getByRole('button', { name: /send/i })
    fireEvent.click(send)
    fireEvent.click(send)
    fireEvent.click(send)
    await waitFor(() => {
      const posts = requests.filter((r) => r.method === 'POST')
      expect(posts.length).toBe(1)
    })
  })

  it('sends on Enter and inserts a newline on Shift+Enter', async () => {
    const requests = stubFetch({ sent: { status: 'RECORDED', resumed: false, task_id: 'AIR-2026-000042' } })
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    const box = await screen.findByLabelText(/reply to the agent about this task/i)
    fireEvent.change(box, { target: { value: 'one line' } })

    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(requests.filter((r) => r.method === 'POST').length).toBe(0)

    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => {
      expect(requests.filter((r) => r.method === 'POST').length).toBe(1)
    })
  })

  it('keeps the send button disabled for an empty message', async () => {
    stubFetch({})
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    const send = await screen.findByRole('button', { name: /send/i })
    expect((send as HTMLButtonElement).disabled).toBe(true)
  })
})

// ======================================================================
// 5. Waiting-for-reply indicator
// ======================================================================
describe('waiting for reply', () => {
  it('marks a task that is blocked on the operator', async () => {
    stubFetch({})
    renderInbox()
    expect(await screen.findByText('Reply')).toBeTruthy()
    fireEvent.click(screen.getByText('Reply').closest('button') as HTMLButtonElement)
    expect(await screen.findByText('Waiting for your reply')).toBeTruthy()
  })

  it('does not mark a task whose blocker was already cleared', async () => {
    stubFetch({
      list: list({ conversations: [conversation({ requires_reply: false })], needs_reply: 0 }),
    })
    renderInbox()
    await screen.findByText('Join the announcement channel')
    expect(screen.queryByText('Reply')).toBeNull()
  })
})

// ======================================================================
// 6. WebSocket updates
// ======================================================================
describe('live updates', () => {
  it('ignores WebSocket events for other subsystems', async () => {
    const events: Array<(e: unknown) => void> = []
    class FakeSocket {
      onopen: (() => void) | null = null
      onmessage: ((m: { data: string }) => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      readyState = 1
      close() {}
      send() {}
      constructor() {
        events.push((e) => this.onmessage?.({ data: JSON.stringify(e) }))
      }
    }
    vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
    const requests = stubFetch({})

    renderInbox()
    await screen.findByText('Join the announcement channel')
    const before = requests.length

    events.forEach((emit) =>
      emit({ type: 'live.event', payload: { integration: 'browser' }, at: '2026-10-13T10:00:00+00:00' }),
    )
    await new Promise((r) => setTimeout(r, 30))
    expect(requests.length).toBe(before)
  })

  it('refreshes the list when the backend announces an inbox event', async () => {
    const emitters: Array<(e: unknown) => void> = []
    class FakeSocket {
      onopen: (() => void) | null = null
      onmessage: ((m: { data: string }) => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      readyState = 1
      close() {}
      send() {}
      constructor() {
        emitters.push((e) => this.onmessage?.({ data: JSON.stringify(e) }))
      }
    }
    vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
    const requests = stubFetch({})

    renderInbox()
    await screen.findByText('Join the announcement channel')
    const listCalls = () => requests.filter((r) => r.url.includes('/inbox/conversations')).length
    const before = listCalls()

    emitters.forEach((emit) =>
      emit({
        type: 'agent.inbox',
        payload: { event: 'intervention.created', conversation_id: 'other', task_id: 'AIR-2026-000099' },
        at: '2026-10-13T10:00:00+00:00',
      }),
    )
    await waitFor(() => {
      expect(listCalls()).toBeGreaterThan(before)
    })
  })
})

// ======================================================================
// 7 & 9. Unread tracking and deep linking
// ======================================================================
describe('unread and deep links', () => {
  it('marks a conversation seen once it is opened', () => {
    markSeen('c1', '2026-10-13T11:00:00+00:00')
    expect(isUnread('c1', '2026-10-13T10:00:00+00:00')).toBe(false)
    expect(isUnread('c1', '2026-10-13T12:00:00+00:00')).toBe(true)
  })

  it('treats a never-opened conversation with messages as unread', () => {
    expect(isUnread('never', '2026-10-13T10:00:00+00:00')).toBe(true)
    expect(isUnread('never', null)).toBe(false)
  })

  it('opens the conversation named in the URL', async () => {
    stubFetch({})
    renderInbox('/inbox?conversation=c1')
    expect(await screen.findByText('Waiting for your reply')).toBeTruthy()
  })
})

// ======================================================================
// 10. No fake controls
// ======================================================================
describe('honesty', () => {
  it('offers no retry control and no claim of manual resume', async () => {
    stubFetch({})
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    await screen.findByText('Action needed')
    expect(screen.queryByRole('button', { name: /^retry$/i })).toBeNull()
    expect(document.body.innerHTML).not.toMatch(/press retry/i)
  })

  it('does not fabricate a percentage when the API reported no progress', async () => {
    stubFetch({ task: { ...TASK_REPORT, progress: undefined } })
    renderInbox()
    fireEvent.click(await screen.findByRole('button', { name: /join the announcement channel/i }))
    // Wait for the conversation to actually load, then the task report behind it.
    await screen.findByText('Waiting for your reply')
    await waitFor(() => expect(detailsPanel().getByText(/Progress unavailable/)).toBeTruthy())
    expect(document.body.innerHTML).not.toMatch(/NaN/)
  })
})

// ======================================================================
// 11. Deleting a conversation
// ======================================================================
/** A finished task whose request was answered: the case the Delete button exists for. */
function finishedDetail(): InboxConversationDetail {
  return detail({
    task_status: 'COMPLETED',
    requires_reply: false,
    open_request: null,
    task: { ...detail().task!, status: 'COMPLETED' },
  })
}

function deleteButton() {
  return screen.getByRole('button', { name: /^delete$/i }) as HTMLButtonElement
}

describe('deleting a conversation', () => {
  it('asks for confirmation and sends nothing when the operator declines', async () => {
    const requests = stubFetch({ detail: finishedDetail() })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderInbox('/inbox?conversation=c1')
    await screen.findByText('AIR-2026-000042', { selector: 'span' })
    await waitFor(() => expect(deleteButton().disabled).toBe(false))

    fireEvent.click(deleteButton())

    expect(confirm).toHaveBeenCalledTimes(1)
    expect(String(confirm.mock.calls[0][0])).toMatch(/task itself is not changed/i)
    expect(requests.filter((r) => r.method === 'DELETE')).toHaveLength(0)
  })

  it('deletes only the conversation and never touches the task', async () => {
    const requests = stubFetch({ detail: finishedDetail() })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderInbox('/inbox?conversation=c1')
    await waitFor(() => expect(deleteButton().disabled).toBe(false))

    fireEvent.click(deleteButton())

    await waitFor(() =>
      expect(requests.filter((r) => r.method === 'DELETE')).toHaveLength(1),
    )
    const deletes = requests.filter((r) => r.method === 'DELETE')
    expect(deletes[0].url).toMatch(/\/inbox\/conversations\/c1$/)
    // No call may modify, cancel or delete the task.
    const taskWrites = requests.filter((r) => /\/tasks\//.test(r.url) && r.method !== 'GET')
    expect(taskWrites).toEqual([])

    // The deleted conversation is closed rather than left on screen.
    expect(await screen.findByText('Select a conversation')).toBeTruthy()
  })

  it('cannot delete a conversation that is still waiting on the operator', async () => {
    const requests = stubFetch({})
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderInbox('/inbox?conversation=c1')
    await screen.findByText('Waiting for your reply')

    expect(deleteButton().disabled).toBe(true)
    fireEvent.click(deleteButton())

    expect(confirm).not.toHaveBeenCalled()
    expect(requests.filter((r) => r.method === 'DELETE')).toHaveLength(0)
  })

  it('shows the server refusal and keeps the conversation when the delete fails', async () => {
    stubFetch({
      detail: finishedDetail(),
      deleteStatus: 409,
      deleteBody: { detail: 'Task AIR-2026-000042 is still waiting on your input in this conversation.' },
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderInbox('/inbox?conversation=c1')
    await waitFor(() => expect(deleteButton().disabled).toBe(false))

    fireEvent.click(deleteButton())

    expect(await screen.findByText(/still waiting on your input/i)).toBeTruthy()
    // Still open: nothing was removed, so nothing is hidden.
    expect(screen.queryByText('Select a conversation')).toBeNull()
  })
})
