/**
 * Persistent store for the AI Agent Console conversation.
 *
 * Lives outside React so the chat survives navigating to another page (the
 * page component unmounts) and a browser reload (localStorage). A reply that
 * arrives while the operator is on another page is still saved.
 *
 * Each agent message keeps the backend's structured report - the tools that ran,
 * the approvals now outstanding, the blockers - alongside the prose. The UI renders
 * what the backend actually did; it never decides on its own that something
 * succeeded, and the prose alone is never treated as a result.
 */

import type { AgentApprovalRequest, AgentToolCall } from '@/types'

export interface ChatMessage {
  id: string
  role: 'user' | 'agent'
  content: string
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

interface ChatState {
  messages: ChatMessage[]
  pending: boolean
}

export const CHAT_STORAGE_KEY = 'airdropos.agentChat.v1'
const MAX_MESSAGES = 200

/**
 * Drop the bulky parts before persisting.
 *
 * A `web_fetch` result carries a page of text, and keeping 200 of those in
 * localStorage would blow the quota and start throwing on every message. The
 * tool name, status and error are what the operator needs on reload; the payload
 * is re-fetchable from the backend if it is ever needed.
 */
function slim(message: ChatMessage): ChatMessage {
  if (!message.toolCalls) return message
  return {
    ...message,
    toolCalls: message.toolCalls.map((call) => ({
      ...call,
      data: {},
    })),
  }
}

function load(): ChatMessage[] {
  try {
    const raw = localStorage.getItem(CHAT_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as ChatMessage[]).slice(-MAX_MESSAGES) : []
  } catch {
    return []
  }
}

let state: ChatState = { messages: load(), pending: false }
const listeners = new Set<() => void>()
// Bumped every time the conversation is cleared. A request that was already in flight remembers
// the value it started with, so its late reply can be recognised as belonging to a cleared chat.
let epoch = 0

// The same conversation can be open in several tabs. Without this, a tab that was not the one
// clearing keeps showing (and re-saving) the old messages, which brings the chat back.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key !== null && event.key !== CHAT_STORAGE_KEY) return
    state = { ...state, messages: load() }
    listeners.forEach((listener) => listener())
  })
}

function commit(next: ChatState): void {
  state = next
  try {
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(state.messages.map(slim)))
  } catch {
    // Storage full or blocked: keep working in memory.
  }
  listeners.forEach((listener) => listener())
}

export const agentChatStore = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
  getSnapshot(): ChatState {
    return state
  },
  add(message: Omit<ChatMessage, 'id' | 'at'>): void {
    const full: ChatMessage = {
      ...message,
      id: crypto.randomUUID(),
      at: new Date().toISOString(),
    }
    commit({ ...state, messages: [...state.messages, full].slice(-MAX_MESSAGES) })
  },
  setPending(pending: boolean): void {
    // Pending is intentionally not persisted: it only describes this session.
    state = { ...state, pending }
    listeners.forEach((listener) => listener())
  },
  /** Changes whenever the chat is cleared; see `epoch` above. */
  getEpoch(): number {
    return epoch
  },
  clear(): void {
    epoch += 1
    state = { messages: [], pending: false }
    try {
      // Remove the key outright rather than storing "[]", so nothing of the chat stays behind.
      localStorage.removeItem(CHAT_STORAGE_KEY)
    } catch {
      // ignore
    }
    listeners.forEach((listener) => listener())
  },
  /** Called on logout so the next operator never sees this conversation. */
  reset(): void {
    epoch += 1
    state = { messages: [], pending: false }
    try {
      localStorage.removeItem(CHAT_STORAGE_KEY)
    } catch {
      // ignore
    }
    listeners.forEach((listener) => listener())
  },
}