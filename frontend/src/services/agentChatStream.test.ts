import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'
import { agentChatStore } from './agentChatStore'
import type { AgentStreamEvent } from '@/types'

const encoder = new TextEncoder()

/** A fetch Response whose body delivers `chunks` one at a time, like a network stream. */
function sseResponse(chunks: string[], init: ResponseInit = { status: 200 }): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(stream, {
    ...init,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

const frame = (event: Record<string, unknown>) =>
  `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`

const DONE = {
  type: 'done',
  reply: 'Your address is 0xabc.',
  tool_calls: [],
  task: null,
  approvals_required: [],
  blockers: [],
  needs_user_action: [],
  verified: [],
  usage: {},
  truncated: false,
}

describe('api.agentChatStream', () => {
  beforeEach(() => agentChatStore.reset())
  afterEach(() => vi.unstubAllGlobals())

  it('reports live events and resolves with the final result', async () => {
    // Split mid-frame to prove frames are reassembled across chunks.
    const text =
      frame({ type: 'round_start', round: 1 }) +
      frame({ type: 'reply_delta', round: 1, text: 'Your ' }) +
      frame({ type: 'reply_delta', round: 1, text: 'address' }) +
      frame(DONE)
    const cut = Math.floor(text.length / 2)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([text.slice(0, cut), text.slice(cut)])))

    const seen: AgentStreamEvent[] = []
    const result = await api.agentChatStream('hi', [], (e) => seen.push(e))

    expect(seen.map((e) => e.type)).toEqual(['round_start', 'reply_delta', 'reply_delta', 'done'])
    expect(result.reply).toBe('Your address is 0xabc.')
    expect((result as unknown as { type?: string }).type).toBeUndefined()
  })

  it('sends the conversation id (and rewind count) so the server keeps each chat separate', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => sseResponse([frame(DONE)]))
    vi.stubGlobal('fetch', fetchMock)

    await api.agentChatStream('hi', [], () => {}, undefined, undefined, undefined, 'chat-A', 4)
    await api.agentChatStream('hi', [], () => {})

    const withId = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(withId.conversation_id).toBe('chat-A')
    expect(withId.keep_messages).toBe(4)
    const without = JSON.parse(fetchMock.mock.calls[1][1].body)
    expect(without.conversation_id).toBeUndefined()
    expect(without.keep_messages).toBeUndefined()
  })

  it('rejects with the backend message when the stream reports an error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseResponse([frame({ type: 'error', error: 'AGENT_LOOP_FAILED: boom', retryable: true })]),
      ),
    )
    await expect(api.agentChatStream('hi', [], () => {})).rejects.toThrow('AGENT_LOOP_FAILED: boom')
  })

  it('does not pretend a cut-off connection finished', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseResponse([frame({ type: 'reply_delta', round: 1, text: 'Par' })])),
    )
    await expect(api.agentChatStream('hi', [], () => {})).rejects.toThrow(/closed before the agent finished/)
  })

  it('falls back to the blocking endpoint on a backend without streaming', async () => {
    const blocking = new Response(JSON.stringify({ ...DONE, type: undefined }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('Not Found', { status: 404 }))
      .mockResolvedValueOnce(blocking)
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.agentChatStream('hi', [], () => {})

    expect(result.reply).toBe('Your address is 0xabc.')
    expect(String(fetchMock.mock.calls[1][0])).toMatch(/\/agent\/chat$/)
  })
})

describe('agentChatStore live preview', () => {
  beforeEach(() => agentChatStore.reset())

  it('builds the preview from events and drops it when the turn ends', () => {
    const epoch = agentChatStore.getEpoch()
    agentChatStore.setPending(true)
    agentChatStore.applyStreamEvent({ type: 'round_start', round: 1 }, epoch)
    agentChatStore.applyStreamEvent({ type: 'reply_delta', round: 1, text: 'Check' }, epoch)
    agentChatStore.applyStreamEvent({ type: 'reply_delta', round: 1, text: 'ing' }, epoch)
    agentChatStore.applyStreamEvent({ type: 'tool_start', round: 1, tool: 'wallet_address' }, epoch)
    expect(agentChatStore.getSnapshot().live?.text).toBe('Checking')
    expect(agentChatStore.getSnapshot().live?.tools[0].status).toBe('running')

    agentChatStore.applyStreamEvent(
      { type: 'tool_result', round: 1, tool: 'wallet_address', status: 'success', duration_ms: 12 },
      epoch,
    )
    expect(agentChatStore.getSnapshot().live?.tools[0]).toMatchObject({ status: 'success', durationMs: 12 })

    // A new round starts a fresh answer but keeps the tools already run.
    agentChatStore.applyStreamEvent({ type: 'round_start', round: 2 }, epoch)
    expect(agentChatStore.getSnapshot().live?.text).toBe('')
    expect(agentChatStore.getSnapshot().live?.tools).toHaveLength(1)

    agentChatStore.setPending(false)
    expect(agentChatStore.getSnapshot().live).toBeNull()
  })

  it('ignores events from a conversation that was cleared', () => {
    const staleEpoch = agentChatStore.getEpoch()
    agentChatStore.clear()
    agentChatStore.applyStreamEvent({ type: 'reply_delta', round: 1, text: 'late' }, staleEpoch)
    expect(agentChatStore.getSnapshot().live).toBeNull()
  })

  it('aborts the stream in flight when the chat is cleared', () => {
    const abort = vi.fn()
    agentChatStore.setAbort(abort)
    agentChatStore.clear()
    expect(abort).toHaveBeenCalledTimes(1)
  })
})
