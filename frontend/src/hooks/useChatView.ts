/**
 * The state of a message-based session's chat view (Telegram), shared by every place that shows it.
 *
 * The Live Broadcast card and the full-screen page render the SAME session, so they must agree on
 * which chat is open and whether clicking is on. Keeping it here (not in each component) means a chat
 * opened in the card is still open in full screen, and a chat picked in full screen is still there
 * after going back to the card.
 *
 * The open chat survives a page reload (sessionStorage). Click mode never does: it always starts off.
 */

import { useCallback, useSyncExternalStore } from 'react'

interface ChatView {
  peer: string | null
  click: boolean
}

const EMPTY: ChatView = { peer: null, click: false }
const views = new Map<string, ChatView>()
const listeners = new Set<() => void>()
const idOf = (integration: string, key: string) => `${integration}:${key}`
const subscribe = (fn: () => void) => {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function read(id: string): ChatView {
  let v = views.get(id)
  if (!v) {
    let peer: string | null = null
    try {
      const raw = sessionStorage.getItem(`chatview:${id}`)
      if (raw && /^-?\d{1,20}$/.test(raw)) peer = raw
    } catch {
      /* storage unavailable: start empty */
    }
    v = { peer, click: false }
    views.set(id, v)
  }
  return v
}

function write(id: string, patch: Partial<ChatView>) {
  const next = { ...read(id), ...patch }
  views.set(id, next)
  if ('peer' in patch) {
    try {
      if (next.peer) sessionStorage.setItem(`chatview:${id}`, next.peer)
      else sessionStorage.removeItem(`chatview:${id}`)
    } catch {
      /* ignore */
    }
  }
  listeners.forEach((l) => l())
}

export function useChatView(integration: string, key: string) {
  const id = idOf(integration, key)
  const view = useSyncExternalStore(subscribe, () => read(id), () => EMPTY)
  const setPeer = useCallback((peer: string | null) => write(id, { peer }), [id])
  const setClick = useCallback((click: boolean) => write(id, { click }), [id])
  const toggleClick = useCallback(() => write(id, { click: !read(id).click }), [id])
  return { peer: view.peer, click: view.click, setPeer, setClick, toggleClick }
}
