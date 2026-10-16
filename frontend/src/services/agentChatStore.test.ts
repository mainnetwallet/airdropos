import { beforeEach, describe, expect, it, vi } from 'vitest'
import { agentChatStore, CHATS_STORAGE_KEY, CHAT_STORAGE_KEY } from './agentChatStore'

const say = (content: string, conversationId?: string) =>
  agentChatStore.add({ role: 'user', content }, conversationId)

describe('conversations', () => {
  beforeEach(() => agentChatStore.reset())

  it('starts empty and lists a conversation only once it has messages', () => {
    expect(agentChatStore.getSnapshot().conversations).toEqual([])
    say('first question')
    const { conversations, activeId } = agentChatStore.getSnapshot()
    expect(conversations).toHaveLength(1)
    expect(conversations[0]).toMatchObject({ id: activeId, title: 'first question', messageCount: 1 })
  })

  it('new chat keeps the old one and opens a blank one; switching restores it', () => {
    say('alpha')
    const first = agentChatStore.getSnapshot().activeId
    agentChatStore.newConversation()
    expect(agentChatStore.getSnapshot().messages).toEqual([])
    say('beta')
    expect(agentChatStore.getSnapshot().conversations.map((c) => c.title).sort()).toEqual(['alpha', 'beta'])
    agentChatStore.switchTo(first)
    expect(agentChatStore.getSnapshot().messages[0].content).toBe('alpha')
  })

  it('new chat does nothing when the open conversation is already empty', () => {
    const id = agentChatStore.getSnapshot().activeId
    agentChatStore.newConversation()
    expect(agentChatStore.getSnapshot().activeId).toBe(id)
  })

  it('a reply lands in the conversation the turn started in, not the one now open', () => {
    say('question')
    const origin = agentChatStore.getSnapshot().activeId
    agentChatStore.newConversation()
    agentChatStore.add({ role: 'agent', content: 'answer' }, origin)
    expect(agentChatStore.getSnapshot().messages).toEqual([]) // still looking at the new chat
    agentChatStore.switchTo(origin)
    expect(agentChatStore.getSnapshot().messages.map((m) => m.content)).toEqual(['question', 'answer'])
  })

  it('shows the live preview only in the conversation the turn runs in', () => {
    say('q')
    const origin = agentChatStore.getSnapshot().activeId
    const epoch = agentChatStore.getEpoch()
    agentChatStore.setPending(true, origin)
    agentChatStore.applyStreamEvent({ type: 'reply_delta', round: 1, text: 'hi' }, epoch)
    expect(agentChatStore.getSnapshot()).toMatchObject({ pending: true, busy: true })
    agentChatStore.newConversation()
    const other = agentChatStore.getSnapshot()
    expect(other).toMatchObject({ pending: false, busy: true, live: null })
    agentChatStore.setPending(false)
  })

  it('deleting the open conversation opens the newest remaining one', () => {
    say('old')
    agentChatStore.newConversation()
    say('new')
    const newest = agentChatStore.getSnapshot().activeId
    agentChatStore.deleteConversation(newest)
    const snap = agentChatStore.getSnapshot()
    expect(snap.activeId).not.toBe(newest)
    expect(snap.messages[0].content).toBe('old')
  })

  it('deleting the conversation a turn runs in aborts it and drops its late reply', () => {
    say('q')
    const origin = agentChatStore.getSnapshot().activeId
    const abort = vi.fn()
    agentChatStore.setAbort(abort)
    agentChatStore.setPending(true, origin)
    const epoch = agentChatStore.getEpoch()
    agentChatStore.deleteConversation(origin)
    expect(abort).toHaveBeenCalled()
    expect(agentChatStore.getEpoch()).toBe(epoch + 1)
    agentChatStore.add({ role: 'agent', content: 'late' }, origin)
    expect(agentChatStore.getSnapshot().conversations).toEqual([])
    agentChatStore.setAbort(null)
  })

  it('truncateFrom removes a message and everything after it', () => {
    say('one')
    agentChatStore.add({ role: 'agent', content: 'two' })
    say('three')
    const second = agentChatStore.getSnapshot().messages[1].id
    agentChatStore.truncateFrom(second)
    expect(agentChatStore.getSnapshot().messages.map((m) => m.content)).toEqual(['one'])
  })

  it('persists conversations and removes every key when nothing is left', () => {
    say('keep me')
    expect(JSON.parse(localStorage.getItem(CHATS_STORAGE_KEY) as string).conversations).toHaveLength(1)
    agentChatStore.clear()
    expect(localStorage.getItem(CHATS_STORAGE_KEY)).toBeNull()
  })

  it('does not persist photo thumbnails', () => {
    agentChatStore.add({
      role: 'user',
      content: 'look',
      attachments: [{ name: 'a.jpg', kind: 'image', thumb: 'data:image/jpeg;base64,AAAA' }],
    })
    expect(localStorage.getItem(CHATS_STORAGE_KEY)).not.toContain('base64')
  })
})

describe('migration from the single-conversation store', () => {
  it('turns the old saved chat into one conversation', async () => {
    localStorage.clear()
    localStorage.setItem(
      CHAT_STORAGE_KEY,
      JSON.stringify([
        { id: '1', role: 'user', content: 'old question', at: '2026-01-01T00:00:00.000Z' },
        { id: '2', role: 'agent', content: 'old answer', at: '2026-01-01T00:00:01.000Z' },
      ]),
    )
    vi.resetModules()
    const fresh = await import('./agentChatStore')
    const snap = fresh.agentChatStore.getSnapshot()
    expect(snap.conversations).toHaveLength(1)
    expect(snap.conversations[0].title).toBe('old question')
    expect(snap.messages).toHaveLength(2)
  })
})
