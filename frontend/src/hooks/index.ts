/** Real-time WebSocket event hook with automatic reconnection. */

import { useEffect, useRef, useState } from 'react'
import {
  isLiveConnected,
  subscribeConnectionStatus,
  subscribeLiveEvents,
} from '@/services/liveSocket'
import type { WsEvent } from '@/types'

/**
 * Subscribe to the dashboard's live event stream.
 *
 * The socket itself is owned by `@/services/liveSocket` and shared by every caller
 * in this tab, so mounting several of these does not open several connections.
 * This hook only keeps the caller's own view of the stream.
 */
export function useLiveEvents(
  onEvent?: (event: WsEvent) => void,
  options: { keepEvents?: boolean } = {},
) {
  const keepEvents = options.keepEvents === true
  const [connected, setConnected] = useState(isLiveConnected())
  const [events, setEvents] = useState<WsEvent[]>([])
  const handlerRef = useRef(onEvent)
  handlerRef.current = onEvent

  useEffect(() => {
    const releaseEvents = subscribeLiveEvents((event) => {
      // Buffering is opt-in: a state update here re-renders the caller on EVERY frame,
      // which for the dashboard shell and the Inbox page meant a full re-render per event.
      if (keepEvents) setEvents((prev) => [...prev.slice(-99), event])
      handlerRef.current?.(event)
    })
    const releaseStatus = subscribeConnectionStatus(setConnected)
    // The socket may already be open (a sibling component subscribed first).
    setConnected(isLiveConnected())
    return () => {
      releaseEvents()
      releaseStatus()
    }
  }, [keepEvents])

  return { connected, events, latest: events[events.length - 1] }
}

const noopHandler = () => undefined

/**
 * Connection state only. Keeps the shared socket open (a subscriber is required for that)
 * but never re-renders on an incoming event, only when the connection goes up or down.
 */
export function useLiveConnection(): boolean {
  return useLiveEvents(noopHandler).connected
}