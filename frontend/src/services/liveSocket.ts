/**
 * The dashboard's single live-event connection.
 *
 * WHY THIS EXISTS
 * `useLiveEvents` used to own its own `new WebSocket(...)`, and three components
 * call it: `DashboardLayout`, `AgentInbox` and `useLiveFeed`. Every one of those
 * opened a separate socket, so a single browser tab on the Inbox page held two
 * live connections, each with its own 25-second heartbeat and its own reconnect
 * timer. The server logs one `[accepted]` plus one `connection open` per socket, so
 * the terminal showed a pair of lines per subscription rather than one per tab.
 *
 * React StrictMode made it look worse. In development it mounts, runs the effect,
 * tears it down and runs it again, so a single subscription produced the pair twice:
 * connect, close, connect. That is StrictMode working as designed and is not a bug,
 * but it is indistinguishable on the terminal from a real duplicate connection.
 *
 * The fix is to share one socket per browser tab and let components subscribe to it:
 *
 *   * the first subscriber opens the connection, the last one closes it;
 *   * closing is deferred by a short grace period, so a StrictMode remount or a
 *     route change reuses the open socket instead of reconnecting;
 *   * heartbeat, reconnect backoff and auth gating exist once, not once per caller;
 *   * the module is per tab, so a second tab still gets its own connection, which
 *     is legitimate and still reaches the server's fan-out bus.
 *
 * Nothing about authentication changes: `canOpenLiveStream()` is still the only
 * thing that decides whether a connection may be attempted, and the server still
 * resolves and revalidates every session itself.
 */

import { canOpenLiveStream, wsProtocols, wsUrl } from '@/services/api'
import type { WsEvent } from '@/types'

/** Keep-alive ping interval. Must stay below the server's own revalidation loop. */
const HEARTBEAT_MS = 25_000
/** Reconnect backoff: 1s, 2s, 4s ... capped so a downed backend is not hammered. */
const MAX_RECONNECT_MS = 30_000
/**
 * Close is deferred to the end of the current task, not to a timer.
 *
 * React StrictMode mounts, tears down and remounts an effect synchronously within
 * one commit, so a microtask sees the final subscriber set and the socket is kept.
 * A timer would cover StrictMode too, but only by guessing how long to wait: it
 * would keep a dead socket open long after the dashboard was gone, and the delay
 * would have to be tuned against test isolation rather than real behaviour.
 */
function closeWhenIdle(): void {
  queueMicrotask(() => {
    if (eventHandlers.size === 0) teardown()
  })
}

type EventHandler = (event: WsEvent) => void
type StatusHandler = (connected: boolean) => void

let socket: WebSocket | null = null
let heartbeat: number | undefined
let reconnectTimer: number | undefined
/** Consecutive failed attempts; reset on a successful open. */
let retries = 0
let connected = false

const eventHandlers = new Set<EventHandler>()
const statusHandlers = new Set<StatusHandler>()

function setConnected(value: boolean): void {
  if (connected === value) return
  connected = value
  for (const handler of [...statusHandlers]) handler(value)
}

function stopHeartbeat(): void {
  if (heartbeat !== undefined) {
    window.clearInterval(heartbeat)
    heartbeat = undefined
  }
}

function clearReconnect(): void {
  if (reconnectTimer !== undefined) {
    window.clearTimeout(reconnectTimer)
    reconnectTimer = undefined
  }
}

/** Close and forget the socket without scheduling a reconnect. */
function teardown(): void {
  clearReconnect()
  stopHeartbeat()
  const current = socket
  socket = null
  if (current) {
    // Detach first: an intentional close must not be mistaken for a dropped
    // connection and trigger the reconnect path.
    current.onopen = null
    current.onmessage = null
    current.onerror = null
    current.onclose = null
    if (current.readyState === WebSocket.OPEN || current.readyState === WebSocket.CONNECTING) {
      current.close()
    }
  }
  setConnected(false)
}

function scheduleReconnect(): void {
  if (reconnectTimer !== undefined) return
  // Nothing is listening: do not hold a connection open for a closed page.
  if (eventHandlers.size === 0) return
  // A token is required and absent, so every attempt would be a guaranteed
  // rejection. The connection is opened when the dashboard remounts after login.
  if (!canOpenLiveStream()) return

  const delay = Math.min(1000 * 2 ** retries, MAX_RECONNECT_MS)
  retries += 1
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = undefined
    open()
  }, delay)
}

function open(): void {
  if (socket !== null) return
  if (!canOpenLiveStream()) return

  const current = new WebSocket(wsUrl(), wsProtocols())
  socket = current

  current.onopen = () => {
    if (socket !== current) return
    setConnected(true)
    retries = 0
    stopHeartbeat()
    heartbeat = window.setInterval(() => {
      if (current.readyState === WebSocket.OPEN) current.send('ping')
    }, HEARTBEAT_MS)
  }

  current.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data) as WsEvent
      for (const handler of [...eventHandlers]) handler(event)
    } catch {
      // Ignore malformed frames rather than crashing the dashboard.
    }
  }

  current.onclose = () => {
    if (socket !== current) return // already replaced or torn down
    socket = null
    stopHeartbeat()
    setConnected(false)
    scheduleReconnect()
  }

  current.onerror = () => current.close()
}

/**
 * Listen for live events. Returns an unsubscribe function.
 *
 * The first listener opens the shared socket; the last one to leave closes it, at
 * the end of the current task.
 */
export function subscribeLiveEvents(handler: EventHandler): () => void {
  eventHandlers.add(handler)
  if (socket === null) open()

  let released = false
  return () => {
    // Idempotent: React can run an effect cleanup more than once, and a double
    // release would drop a sibling's subscription.
    if (released) return
    released = true
    eventHandlers.delete(handler)
    if (eventHandlers.size === 0) closeWhenIdle()
  }
}

/** Observe the shared connection's state. Returns an unsubscribe function. */
export function subscribeConnectionStatus(handler: StatusHandler): () => void {
  statusHandlers.add(handler)
  let released = false
  return () => {
    if (released) return
    released = true
    statusHandlers.delete(handler)
  }
}

/** Current state of the shared socket. Read once to seed component state. */
export function isLiveConnected(): boolean {
  return connected
}