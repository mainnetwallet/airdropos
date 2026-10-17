/**
 * Tracks which task conversations the operator has actually read.
 *
 * Local only, and deliberately so: it drives an unread dot, nothing else. Whether a
 * conversation needs a reply always comes from the backend, and message history always
 * comes from the API. Losing this store costs a few stray dots, never a message.
 */

const STORAGE_KEY = 'airdropos.agentInbox.seen.v1'

// Enough history that a conversation reopened after a long gap is still marked seen.
const MAX_TRACKED = 300

type Seen = Record<string, string>

let seen: Seen = load()
const listeners = new Set<() => void>()

function load(): Seen {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    // Keep only well-formed entries so a corrupted store cannot leak into the UI.
    return Object.fromEntries(
      Object.entries(parsed as Seen).filter(
        ([id, at]) => typeof id === 'string' && typeof at === 'string',
      ),
    )
  } catch {
    return {}
  }
}

function commit(next: Seen) {
  seen = next
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(seen))
  } catch {
    // A full or unavailable store must not break the page.
  }
  listeners.forEach((listener) => listener())
}

export function markSeen(conversationId: string, at: string) {
  if (!conversationId) return
  const next = { ...seen, [conversationId]: at }
  const ids = Object.keys(next)
  if (ids.length > MAX_TRACKED) {
    // Drop the least recently seen entries.
    ids
      .sort((a, b) => new Date(next[a]).getTime() - new Date(next[b]).getTime())
      .slice(0, ids.length - MAX_TRACKED)
      .forEach((id) => delete next[id])
  }
  commit(next)
}

/**
 * A conversation is unread when it holds a message newer than the last time the operator
 * opened it, or when it has messages and has never been opened.
 */
export function isUnread(conversationId: string, lastMessageAt: string | null): boolean {
  const last = seen[conversationId]
  if (!lastMessageAt) return false
  if (!last) return true
  return new Date(lastMessageAt).getTime() > new Date(last).getTime()
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function reset() {
  commit({})
}