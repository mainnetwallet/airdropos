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

interface ChatState {
  messages: ChatMessage[]
  pending: boolean
  live: LiveTurn | null
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

let state: ChatState = { messages: load(), pending: false, live: null }
const listeners = new Set<() => void>()
// Bumped every time the conversation is cleared. A request that was already in flight remembers
// the value it started with, so its late reply can be recognised as belonging to a cleared chat.
let epoch = 0
// Stops the stream in flight (if any). Called when the chat is cleared or the operator logs
// out, so the backend can end the turn at its next safe boundary instead of finishing a
// conversation nobody can see any more.
let abortStream: (() => void) | null = null

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
  /** Remove the newest message (used by "regenerate" to replace the last reply). */
  removeLast(): void {
    commit({ ...state, messages: state.messages.slice(0, -1) })
  },
  /** Stop the turn in flight; the backend ends it at its next safe boundary. */
  stop(): void {
    abortStream?.()
  },
  setPending(pending: boolean): void {
    // Pending is intentionally not persisted: it only describes this session. The live
    // preview belongs to the turn, so it ends with it.
    state = { ...state, pending, live: pending ? state.live : null }
    listeners.forEach((listener) => listener())
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
    const live: LiveTurn = state.live ?? { round: 0, text: '', tools: [] }
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
    state = { ...state, live: next }
    listeners.forEach((listener) => listener())
  },
  /** Changes whenever the chat is cleared; see `epoch` above. */
  getEpoch(): number {
    return epoch
  },
  clear(): void {
    epoch += 1
    abortStream?.()
    state = { messages: [], pending: false, live: null }
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
    abortStream?.()
    state = { messages: [], pending: false, live: null }
    try {
      localStorage.removeItem(CHAT_STORAGE_KEY)
    } catch {
      // ignore
    }
    listeners.forEach((listener) => listener())
  },
}