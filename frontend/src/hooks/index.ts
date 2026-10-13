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
export function useLiveEvents(onEvent?: (event: WsEvent) => void) {
  const [connected, setConnected] = useState(isLiveConnected())
  const [events, setEvents] = useState<WsEvent[]>([])
  const handlerRef = useRef(onEvent)
  handlerRef.current = onEvent

  useEffect(() => {
    const releaseEvents = subscribeLiveEvents((event) => {
      setEvents((prev) => [...prev.slice(-99), event])
      handlerRef.current?.(event)
    })
    const releaseStatus = subscribeConnectionStatus(setConnected)
    // The socket may already be open (a sibling component subscribed first).
    setConnected(isLiveConnected())
    return () => {
      releaseEvents()
      releaseStatus()
    }
  }, [])

  return { connected, events, latest: events[events.length - 1] }
}