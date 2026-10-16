/**
 * Persistent store for the AI Agent Console conversations.
 *
 * Lives outside React so the chats survive navigating to another page (the
 * page component unmounts) and a browser reload (localStorage). A reply that
 * arrives while the operator is on another page, or has opened a different
 * conversation, is still saved into the conversation it belongs to.
 *
 * Each agent message keeps the backend's structured report - the tools that ran,
 * the approvals now outstanding, the blockers - alongside the prose. The UI renders
 * what the backend actually did; it never decides on its own that something
 * succeeded, and the prose alone is never treated as a result.
 */

import type { AgentApprovalRequest, AgentStreamEvent, AgentToolCall } from '@/types'

export interface ChatMessage {
  id: string
  role: 'user' | 'agent'
  content: string
  /** What the operator typed, when `content` also carries attached-file text. */
  display?: string
  /** Files and photos attached to this message. Thumbnails are not persisted. */
  attachments?: Array<{ name: string; kind: 'image' | 'file'; thumb?: string }>
  at: string
  taskId?: string
  taskStatus?: string
  /** Failed requests are shown to the operator but never sent to the model. */
  error?: boolean
  /** Tools the backend really executed for this message. */
  toolCalls?: AgentToolCall[]
  /** Prepared value-moving operations still awaiting the operator's approval. */
  approvals?: AgentApprovalRequest[]
  /** Things that blocked the request, verbatim from the backend. */
  blockers?: string[]
  /** Questions the operator has to answer before the agent can continue. */
  needsUserAction?: string[]
  /** True when a bound was hit, so the turn stopped early rather than completing. */
  truncated?: boolean
}

/** One tool the agent is running (or has just run) during the turn in progress. */
export interface LiveTool {
  id: number
  tool: string
  /** `running` until the backend reports the outcome; then its real status. */
  status: string
  durationMs?: number
  error?: string | null
}

/**
 * The turn that is streaming right now. A preview only: it is never persisted and never
 * sent back to the model. When the turn ends, the backend's final result replaces it.
 */
export interface LiveTurn {
  round: number
  /** The answer as written so far, for the current round. */
  text: string
  tools: LiveTool[]
}

/** One saved conversation. Its title is derived from its first message, never stored. */
export interface Conversation {
  id: string
  createdAt: string
  updatedAt: string
  messages: ChatMessage[]
}

/** What the history sidebar needs to list a conversation. */
export interface ConversationSummary {
  id: string
  title: string
  updatedAt: string
  messageCount: number
}

interface ChatState {
  /** Messages of the conversation that is open. */
  messages: ChatMessage[]
  /** A turn is running in the OPEN conversation. */
  pending: boolean
  /** A turn is running in some conversation (only one at a time is allowed). */
  busy: boolean
  live: LiveTurn | null
  activeId: string
  /** Conversations that have at least one message, newest first. */
  conversations: ConversationSummary[]
}

/** The old single-conversation key. Read once to migrate, then removed. */
export const CHAT_STORAGE_KEY = 'airdropos.agentChat.v1'
export const CHATS_STORAGE_KEY = 'airdropos.agentChat.v2'
const MAX_MESSAGES = 200
const MAX_CONVERSATIONS = 40
const TITLE_CHARS = 48

/**
 * Drop the bulky parts before persisting.
 *
 * A `web_fetch` result carries a page of text, and keeping 200 of those in
 * localStorage would blow the quota and start throwing on every message. The
 * tool name, status and error are what the operator needs on reload; the payload
 * is re-fetchable from the backend if it is ever needed. Photo thumbnails go too.
 */
function slim(message: ChatMessage): ChatMessage {
  const attachments = message.attachments?.map(({ thumb: _thumb, ...rest }) => rest)
  const base = attachments ? { ...message, attachments } : message
  if (!base.toolCalls) return base
  return {
    ...base,
    toolCalls: base.toolCalls.map((call) => ({
      ...call,
      data: {},
    })),
  }
}

function byNewest(a: Conversation, b: Conversation): number {
  return b.updatedAt.localeCompare(a.updatedAt)
}

function blank(): Conversation {
  const now = new Date().toISOString()
  return { id: crypto.randomUUID(), createdAt: now, updatedAt: now, messages: [] }
}

function titleOf(messages: ChatMessage[]): string {
  const first = messages.find((m) => m.role === 'user')
  const text = (first?.display ?? first?.content ?? '').replace(/\s+/g, ' ').trim()
  if (!text) return 'New chat'
  return text.length > TITLE_CHARS ? `${text.slice(0, TITLE_CHARS - 1)}…` : text
}

function isConversation(value: unknown): value is Conversation {
  const c = value as Conversation | null
  return !!c && typeof c.id === 'string' && Array.isArray(c.messages)
}

function loadAll(): { conversations: Conversation[]; activeId: string } {
  let conversations: Conversation[] = []
  let activeId: string | null = null
  try {
    const raw = localStorage.getItem(CHATS_STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed && Array.isArray(parsed.conversations)) {
        conversations = (parsed.conversations as unknown[]).filter(isConversation).map((c) => ({
          ...c,
          messages: c.messages.slice(-MAX_MESSAGES),
        }))
        activeId = typeof parsed.activeId === 'string' ? parsed.activeId : null
      }
    } else {
      // Migrate the single conversation older versions kept.
      const legacy = localStorage.getItem(CHAT_STORAGE_KEY)
      const parsed = legacy ? JSON.parse(legacy) : null
      if (Array.isArray(parsed) && parsed.length > 0) {
        const messages = (parsed as ChatMessage[]).slice(-MAX_MESSAGES)
        const last = messages[messages.length - 1]
        conversations = [
          {
            id: crypto.randomUUID(),
            createdAt: messages[0]?.at ?? new Date().toISOString(),
            updatedAt: last?.at ?? new Date().toISOString(),
            messages,
          },
        ]
      }
    }
  } catch {
    conversations = []
  }
  if (!activeId || !conversations.some((c) => c.id === activeId)) {
    // Reopen the most recent conversation (this is also how a migrated chat comes back); only an
    // operator with nothing saved starts on a blank one.
    const newest = [...conversations].sort(byNewest)[0]
    if (newest) {
      activeId = newest.id
    } else {
      const fresh = blank()
      conversations = [fresh]
      activeId = fresh.id
    }
  }
  return { conversations, activeId }
}

const initial = loadAll()
let convs: Conversation[] = initial.conversations
let activeId: string = initial.activeId
// The conversation a turn is running in, so its reply lands there even if the operator has
// opened another one meanwhile.
let pendingId: string | null = null
let rawLive: LiveTurn | null = null
let state: ChatState
const listeners = new Set<() => void>()
// Bumped every time a conversation that has a turn in flight is cleared or deleted. A request that
// was already in flight remembers the value it started with, so its late reply can be recognised
// as belonging to a conversation that no longer exists.
let epoch = 0
// Stops the stream in flight (if any). Called when its conversation is cleared or deleted, or the
// operator logs out, so the backend can end the turn at its next safe boundary instead of finishing
// a conversation nobody can see any more.
let abortStream: (() => void) | null = null

function rebuild(): void {
  const open = convs.find((c) => c.id === activeId) as Conversation
  const here = pendingId !== null && pendingId === activeId
  state = {
    messages: open.messages,
    pending: here,
    busy: pendingId !== null,
    live: here ? rawLive : null,
    activeId,
    conversations: convs
      .filter((c) => c.messages.length > 0)
      .sort(byNewest)
      .map((c) => ({
        id: c.id,
        title: titleOf(c.messages),
        updatedAt: c.updatedAt,
        messageCount: c.messages.length,
      })),
  }
}

function persist(): void {
  try {
    const keep = convs.filter((c) => c.messages.length > 0).sort(byNewest).slice(0, MAX_CONVERSATIONS)
    if (keep.length === 0) {
      // Remove the keys outright rather than storing an empty list, so nothing of the chats stays behind.
      localStorage.removeItem(CHATS_STORAGE_KEY)
      localStorage.removeItem(CHAT_STORAGE_KEY)
      return
    }
    let list = keep
    for (;;) {
      try {
        const slimmed = list.map((c) => ({ ...c, messages: c.messages.map(slim) }))
        localStorage.setItem(CHATS_STORAGE_KEY, JSON.stringify({ v: 2, activeId, conversations: slimmed }))
        localStorage.removeItem(CHAT_STORAGE_KEY)
        return
      } catch {
        // Storage full: save fewer (the oldest are dropped from the saved copy only).
        if (list.length <= 1) return
        list = list.slice(0, -1)
      }
    }
  } catch {
    // Storage blocked: keep working in memory.
  }
}

function notify(): void {
  rebuild()
  listeners.forEach((listener) => listener())
}

function commit(): void {
  persist()
  notify()
}

/** Forget empty conversations other than the open one, so "New chat" does not pile up blanks. */
function prune(): void {
  convs = convs.filter((c) => c.messages.length > 0 || c.id === activeId)
}

function setOpenMessages(messages: ChatMessage[]): void {
  convs = convs.map((c) =>
    c.id === activeId ? { ...c, messages, updatedAt: new Date().toISOString() } : c,
  )
  commit()
}

rebuild()

// The same conversations can be open in several tabs. Without this, a tab that was not the one
// clearing keeps showing (and re-saving) the old messages, which brings the chat back.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key !== null && event.key !== CHATS_STORAGE_KEY && event.key !== CHAT_STORAGE_KEY) return
    const loaded = loadAll()
    convs = loaded.conversations
    if (!convs.some((c) => c.id === activeId)) activeId = loaded.activeId
    notify()
  })
}

export const agentChatStore = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
  getSnapshot(): ChatState {
    return state
  },
  /** Append a message. `conversationId` is where a turn started; omitted, the open conversation. */
  add(message: Omit<ChatMessage, 'id' | 'at'>, conversationId?: string): void {
    const target = conversationId ?? activeId
    if (!convs.some((c) => c.id === target)) return // that conversation was deleted meanwhile
    const full: ChatMessage = {
      ...message,
      id: crypto.randomUUID(),
      at: new Date().toISOString(),
    }
    convs = convs.map((c) =>
      c.id === target
        ? { ...c, updatedAt: full.at, messages: [...c.messages, full].slice(-MAX_MESSAGES) }
        : c,
    )
    commit()
  },
  /** Remove the newest message (used by "regenerate" to replace the last reply). */
  removeLast(): void {
    setOpenMessages(state.messages.slice(0, -1))
  },
  /** Remove a message and everything after it (used by "edit and resend"). */
  truncateFrom(messageId: string): void {
    const index = state.messages.findIndex((m) => m.id === messageId)
    if (index < 0) return
    setOpenMessages(state.messages.slice(0, index))
  },
  /** Stop the turn in flight; the backend ends it at its next safe boundary. */
  stop(): void {
    abortStream?.()
  },
  /** Start a fresh conversation. Does nothing if the open one is already empty. */
  newConversation(): void {
    if (state.messages.length === 0) return
    const fresh = blank()
    convs = [...convs, fresh]
    activeId = fresh.id
    prune()
    commit()
  },
  /** Open a saved conversation. */
  switchTo(id: string): void {
    if (id === activeId || !convs.some((c) => c.id === id)) return
    activeId = id
    prune()
    commit()
  },
  deleteConversation(id: string): void {
    if (!convs.some((c) => c.id === id)) return
    if (id === pendingId) {
      epoch += 1
      abortStream?.()
      pendingId = null
      rawLive = null
    }
    convs = convs.filter((c) => c.id !== id)
    if (id === activeId) {
      const next = [...convs].filter((c) => c.messages.length > 0).sort(byNewest)[0]
      if (next) {
        activeId = next.id
      } else {
        const fresh = blank()
        convs = [fresh]
        activeId = fresh.id
      }
    }
    prune()
    commit()
  },
  setPending(pending: boolean, conversationId?: string): void {
    // Pending is intentionally not persisted: it only describes this session. The live
    // preview belongs to the turn, so it ends with it.
    pendingId = pending ? (conversationId ?? activeId) : null
    if (!pending) rawLive = null
    notify()
  },
  /** Register how to abort the stream in flight; pass null once it has ended. */
  setAbort(abort: (() => void) | null): void {
    abortStream = abort
  },
  /**
   * Fold one streamed event into the live preview. `forEpoch` is the epoch the request
   * started in: events of a conversation that has since been cleared are dropped.
   */
  applyStreamEvent(event: AgentStreamEvent, forEpoch: number): void {
    if (forEpoch !== epoch) return
    const live: LiveTurn = rawLive ?? { round: 0, text: '', tools: [] }
    let next: LiveTurn | null = null

    switch (event.type) {
      case 'round_start':
        // A new round starts a fresh answer; tools from earlier rounds stay listed.
        next = { ...live, round: event.round, text: '' }
        break
      case 'reply_delta':
        next = { ...live, text: live.text + event.text }
        break
      case 'tool_start':
        next = {
          ...live,
          tools: [...live.tools, { id: live.tools.length + 1, tool: event.tool, status: 'running' }],
        }
        break
      case 'tool_result': {
        const tools = [...live.tools]
        for (let i = tools.length - 1; i >= 0; i -= 1) {
          if (tools[i].tool === event.tool && tools[i].status === 'running') {
            tools[i] = {
              ...tools[i],
              status: event.status,
              durationMs: event.duration_ms,
              error: event.error ?? null,
            }
            break
          }
        }
        next = { ...live, tools }
        break
      }
      default:
        return // `done` and `error` are handled by the caller
    }
    rawLive = next
    notify()
  },
  /** Changes whenever a conversation with a turn in flight is cleared or deleted; see `epoch`. */
  getEpoch(): number {
    return epoch
  },
  /** Empty the OPEN conversation. */
  clear(): void {
    // A turn running in a different conversation is left alone.
    if (pendingId === null || pendingId === activeId) {
      epoch += 1
      abortStream?.()
      pendingId = null
      rawLive = null
    }
    setOpenMessages([])
  },
  /** Called on logout so the next operator never sees these conversations. */
  reset(): void {
    epoch += 1
    abortStream?.()
    pendingId = null
    rawLive = null
    const fresh = blank()
    convs = [fresh]
    activeId = fresh.id
    persist() // nothing left to keep: removes the stored keys
    notify()
  },
}
