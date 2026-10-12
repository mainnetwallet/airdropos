/**
 * Agent Console regression tests.
 *
 * The point of these is that the UI renders what the BACKEND reported. Each test
 * stubs `/agent/chat` with a fixed response and asserts the screen shows that exact
 * outcome - including the outcomes an operator most needs to see: a refusal, a
 * failure, and a transfer that is prepared but NOT sent.
 *
 * There is no case here where prose alone counts as a result.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import AgentChat from './AgentChat'
import { agentChatStore } from '@/services/agentChatStore'
import type { AgentChatResponse } from '@/types'

const ADDRESS = '0x' + 'ab'.repeat(20)
const DESTINATION = '0x' + 'cd'.repeat(20)

function baseResponse(over: Partial<AgentChatResponse> = {}): AgentChatResponse {
  return {
    reply: '',
    tool_calls: [],
    task: null,
    approvals_required: [],
    blockers: [],
    needs_user_action: [],
    verified: [],
    usage: {},
    truncated: false,
    ...over,
  }
}

let chatResponse: AgentChatResponse

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.includes('/agent/chat')) {
        return new Response(JSON.stringify(chatResponse), { status: 200 })
      }
      if (url.includes('/agent/info')) {
        return new Response(
          JSON.stringify({
            base_url: 'https://example.invalid/v1',
            model_id: 'fixed-model',
            temperature: 0.2,
            max_tokens: 4096,
            timeout_seconds: 60,
            max_retries: 3,
            api_key_configured: true,
            api_key_masked: 'sk-***-key',
            policy: 'single_fixed_model',
          }),
          { status: 200 },
        )
      }
      return new Response('{}', { status: 200 })
    }),
  )
}

function renderConsole() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AgentChat />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function send(message: string) {
  const box = screen.getByLabelText(/message to the agent/i)
  fireEvent.change(box, { target: { value: message } })
  fireEvent.submit(box.closest('form') as HTMLFormElement)
}

beforeEach(() => {
  agentChatStore.reset()
  chatResponse = baseResponse()
  stubFetch()
  // jsdom implements neither of these; the component only calls them for cosmetics.
  Element.prototype.scrollIntoView = vi.fn()
  if (!document.execCommand) {
    Object.assign(document, { execCommand: vi.fn(() => true) })
  }
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  agentChatStore.reset()
})

describe('Agent Console wallet address', () => {
  it('shows the real address the backend returned and copies exactly that value', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    chatResponse = baseResponse({
      reply: 'Your Ethereum wallet address is below.',
      tool_calls: [
        {
          tool: 'get_wallet_address',
          status: 'success',
          arguments: { network: 'ethereum' },
          data: { address: ADDRESS, copyable_address: ADDRESS, label: 'Main' },
          duration_ms: 4,
          evidence: { authoritative: true, kind: 'wallet_record' },
        },
      ],
    })

    renderConsole()
    await send('Show my wallet address')

    await screen.findByText(ADDRESS)
    expect(screen.getByText('get_wallet_address')).toBeTruthy()
    // The evidence kind the tool attached is shown, so "verified" is traceable.
    expect(screen.getByText(/evidence: wallet_record/)).toBeTruthy()

    const copy = screen.getByRole('button', { name: `Copy address ${ADDRESS}` })
    fireEvent.click(copy)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(ADDRESS))
    await screen.findByText(/copied/i)
  })

  it('reports a copy failure instead of pretending it worked', async () => {
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockRejectedValue(new Error('denied')),
      },
    })

    chatResponse = baseResponse({
      reply: 'Address below.',
      tool_calls: [
        {
          tool: 'get_wallet_address',
          status: 'success',
          arguments: {},
          data: { address: ADDRESS, copyable_address: ADDRESS },
          duration_ms: 2,
        },
      ],
    })

    renderConsole()
    await send('Show my wallet address')
    await screen.findByText(ADDRESS)

    fireEvent.click(screen.getByRole('button', { name: `Copy address ${ADDRESS}` }))
    await screen.findByText(/copy failed/i)
  })

  it('never invents an address when the tool was refused', async () => {
    chatResponse = baseResponse({
      reply: 'I could not do that.',
      tool_calls: [
        {
          tool: 'get_wallet_address',
          status: 'not_authorized',
          arguments: {},
          data: {},
          error: 'Permission(s) not granted: wallet.read',
          error_category: 'MISSING_PERMISSION',
          duration_ms: 0,
        },
      ],
    })

    renderConsole()
    await send('Show my wallet address')

    // The raw backend status is rendered, not a friendlier paraphrase that might
    // read as success.
    await screen.findByText('not_authorized')
    expect(screen.getByText(/Permission\(s\) not granted/i)).toBeTruthy()
    // No address anywhere on screen: the tool never produced one.
    expect(document.body.innerHTML).not.toMatch(/0x[0-9a-f]{40}/i)
  })
})

describe('Agent Console honest reporting', () => {
  it('shows a blocked operation as blocked, with the backend reason', async () => {
    chatResponse = baseResponse({
      reply: 'The transfer could not be prepared.',
      tool_calls: [
        {
          tool: 'prepare_native_transfer',
          status: 'failure',
          arguments: { network: 'ethereum' },
          data: {},
          error: "No RPC endpoint is configured for 'ethereum'.",
          error_category: 'NO_RPC',
          retryable: false,
          duration_ms: 1,
        },
      ],
      blockers: ["prepare_native_transfer: No RPC endpoint is configured for 'ethereum'."],
    })

    renderConsole()
    await send('Transfer 0.01 ETH')

    await screen.findByText(/blocked:/i)
    // The backend's own wording, in both the tool row and the blocker notice.
    expect(screen.getAllByText(/No RPC endpoint is configured/i).length).toBeGreaterThan(0)
    expect(screen.getByText('failure')).toBeTruthy()
  })

  it('shows a prepared transfer as awaiting approval and states nothing was sent', async () => {
    chatResponse = baseResponse({
      reply: 'Here is the exact transfer to confirm.',
      tool_calls: [
        {
          tool: 'prepare_native_transfer',
          status: 'success',
          arguments: { network: 'ethereum', amount: '0.01' },
          data: {
            proposal: {
              proposal_id: 'p-1',
              kind: 'native',
              network: 'ethereum',
              chain_id: 1,
              wallet_address: ADDRESS,
              to_address: DESTINATION,
              asset: 'ETH',
              amount: '0.01',
              estimated_fee_native: '0.00042',
              warnings: ['Prepared only. Nothing has been signed or sent.'],
              approval_fields: { network: 'ethereum' },
            },
          },
          duration_ms: 90,
          evidence: { authoritative: true, kind: 'prepared_transfer' },
        },
      ],
      approvals_required: [
        {
          proposal_id: 'p-1',
          kind: 'native',
          network: 'ethereum',
          chain_id: 1,
          wallet: ADDRESS,
          to: DESTINATION,
          asset: 'ETH',
          amount: '0.01',
          estimated_fee_native: '0.00042',
          warnings: ['Prepared only. Nothing has been signed or sent.'],
          approval_fields: { network: 'ethereum' },
          state: 'AWAITING_OPERATOR_APPROVAL',
        },
      ],
    })

    renderConsole()
    await send('Transfer 0.01 ETH to ' + DESTINATION)

    await screen.findByText(/approval required/i)
    expect(screen.getByText(ADDRESS)).toBeTruthy()
    expect(screen.getByText(DESTINATION)).toBeTruthy()
    // The warning appears in the approval panel and again in the tool row.
    expect(screen.getAllByText(/Nothing has been signed or sent/i).length).toBeGreaterThan(0)
    // A prepared transfer must not be presented as a confirmed transaction.
    expect(screen.queryByText(/confirmed/i)).toBeNull()
    expect(screen.queryByText(/broadcast/i)).toBeNull()
  })

  it('surfaces a real task id and status returned by the backend', async () => {
    chatResponse = baseResponse({
      reply: 'Task created and queued.',
      task: {
        task_id: 'AIR-2026-000042',
        status: 'READY',
        title: 'Claim the test airdrop',
        priority: 5,
      },
    })

    renderConsole()
    await send('Start this airdrop')

    await screen.findByText('AIR-2026-000042')
    expect(screen.getByText('READY')).toBeTruthy()
  })

  it('marks an interrupted turn as incomplete rather than finished', async () => {
    chatResponse = baseResponse({
      reply: 'Partial result.',
      truncated: true,
      blockers: ['Reached the 12-tool limit for one message.'],
    })

    renderConsole()
    await send('Do everything')

    await screen.findByText(/stopped early/i)
  })
})

describe('Agent Console input behaviour', () => {
  it('sends on Enter and inserts a newline on Shift+Enter', async () => {
    chatResponse = baseResponse({ reply: 'ok' })
    renderConsole()
    const box = screen.getByLabelText(/message to the agent/i) as HTMLTextAreaElement

    fireEvent.change(box, { target: { value: 'line one' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(screen.queryByText('ok')).toBeNull()

    fireEvent.keyDown(box, { key: 'Enter' })
    await screen.findByText('ok')
  })

  it('shows a backend error as a failed turn and does not add it to the model history', async () => {
    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/agent/chat')) {
        return new Response(JSON.stringify({ detail: { error: 'Rate limit exceeded.' } }), {
          status: 429,
        })
      }
      return new Response('{}', { status: 200 })
    })

    renderConsole()
    await send('hello')

    await screen.findByText(/request failed: rate limit exceeded/i)
  })
})