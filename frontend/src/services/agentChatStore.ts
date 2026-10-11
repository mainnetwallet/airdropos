/**
 * Persistent store for the AI Agent Console conversation.
 *
 * Lives outside React so the chat survives navigating to another page (the
 * page component unmounts) and a browser reload (localStorage). A reply that
 * arrives while the operator is on another page is still saved.
 */

export interface ChatMessage {
  id: string
  role: 'user' | 'agent'
  content: string
  at: string
  taskId?: string
}

interface ChatState {
  messages: ChatMessage[]
  pending: boolean
}

export const CHAT_STORAGE_KEY = 'airdropos.agentChat.v1'
const MAX_MESSAGES = 200

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

function commit(next: ChatState): void {
  state = next
  try {
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(state.messages))
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
  clear(): void {
    commit({ messages: [], pending: state.pending })
  },
  /** Called on logout so the next operator never sees this conversation. */
  reset(): void {
    state = { messages: [], pending: false }
    try {
      localStorage.removeItem(CHAT_STORAGE_KEY)
    } catch {
      // ignore
    }
    listeners.forEach((listener) => listener())
  },
}
