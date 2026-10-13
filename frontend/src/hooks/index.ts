/** Real-time WebSocket event hook with automatic reconnection. */

import { useEffect, useRef, useState } from 'react'
import { canOpenLiveStream, wsProtocols, wsUrl } from '@/services/api'
import type { WsEvent } from '@/types'

export function useLiveEvents(onEvent?: (event: WsEvent) => void) {
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState<WsEvent[]>([])
  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(0)
  const handlerRef = useRef(onEvent)
  handlerRef.current = onEvent

  useEffect(() => {
    let closed = false
    let reconnectTimer: number | undefined
    let heartbeat: number | undefined

    const connect = () => {
      if (closed) return
      // A refused handshake is not a retryable failure: without a session token the
      // server closes the connection before accepting it, so every attempt would be
      // a guaranteed rejection (visible to the operator as a burst of failed
      // connection attempts). Waiting for a token costs nothing and keeps the
      // dashboard quiet. Re-opening is driven by remounting after login, which clears
      // this effect and runs `connect` again with a token present.
      if (!canOpenLiveStream()) {
        setConnected(false)
        return
      }
      const socket = new WebSocket(wsUrl(), wsProtocols())
      socketRef.current = socket

      socket.onopen = () => {
        setConnected(true)
        retryRef.current = 0
        heartbeat = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send('ping')
        }, 25000)
      }

      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as WsEvent
          setEvents((prev) => [...prev.slice(-99), event])
          handlerRef.current?.(event)
        } catch {
          // Ignore malformed frames rather than crashing the dashboard.
        }
      }

      socket.onclose = () => {
        setConnected(false)
        if (heartbeat) window.clearInterval(heartbeat)
        if (closed) return
        // The session may have ended while the socket was open. Reconnecting without
        // a token would only produce another refused handshake.
        if (!canOpenLiveStream()) return
        // Exponential backoff, capped so a downed backend is not hammered.
        const delay = Math.min(1000 * 2 ** retryRef.current, 30000)
        retryRef.current += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }

      socket.onerror = () => socket.close()
    }

    connect()

    return () => {
      closed = true
      if (heartbeat) window.clearInterval(heartbeat)
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      socketRef.current?.close()
    }
  }, [])

  return { connected, events, latest: events[events.length - 1] }
}
