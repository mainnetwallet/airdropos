/** Hooks for Live Broadcast: data feed, frame streaming, exclusive manual control. */

import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, fetchFrame, liveApi as api, releaseControlOnUnload } from '@/services/api'
import { useLiveEvents } from '@/hooks'
import type { WsEvent } from '@/types'
import type { InputEvent, LiveEvent, LiveSession } from '@/types/live'

// ---------------------------------------------------------------- media query
export function useMediaQuery(query: string): boolean {
  const get = () => (typeof window !== 'undefined' ? window.matchMedia(query).matches : false)
  const [matches, setMatches] = useState(get)
  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange()
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])
  return matches
}

// ---------------------------------------------------------------- visibility
export function useInView<T extends Element>(): [RefObject<T>, boolean] {
  const ref = useRef<T>(null)
  const [inView, setInView] = useState(true)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), { rootMargin: '120px' })
    io.observe(el)
    return () => io.disconnect()
  }, [])
  return [ref, inView]
}

// ---------------------------------------------------------------- data feed
const MAX_EVENTS = 200

/**
 * Overview polling + activity events. Events arrive over the existing authenticated
 * WebSocket (`live.event`); polling is the fallback so the page still works if the
 * socket drops.
 */
export function useLiveFeed(opts: { session?: string } = {}) {
  const qc = useQueryClient()
  const [pushed, setPushed] = useState<LiveEvent[]>([])

  const overview = useQuery({
    queryKey: ['live', 'overview'],
    queryFn: api.liveOverview,
    refetchInterval: 3000,
    retry: 1,
  })
  const tail = useQuery({
    queryKey: ['live', 'events', opts.session ?? 'all'],
    queryFn: () => api.liveEvents({ session: opts.session, limit: 100 }),
    refetchInterval: 8000,
  })

  const { connected } = useLiveEvents(
    useCallback(
      (ev: WsEvent) => {
        if (ev.type !== 'live.event') return
        const event = ev.payload as unknown as LiveEvent
        if (opts.session && event.session_id !== opts.session) return
        setPushed((prev) => [...prev.slice(-MAX_EVENTS), event])
        void qc.invalidateQueries({ queryKey: ['live', 'overview'] })
      },
      [opts.session, qc],
    ),
  )

  const events = useMemo(() => {
    const byId = new Map<string, LiveEvent>()
    for (const e of tail.data?.events ?? []) byId.set(e.id, e)
    for (const e of pushed) byId.set(e.id, e)
    return [...byId.values()].sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, MAX_EVENTS)
  }, [tail.data, pushed])

  return { overview, events, socketConnected: connected }
}

/**
 * Whether the data on screen is old. The overview polls every few seconds, so a failed refresh or a
 * long gap means what is shown may no longer be true; callers disable controls and say so.
 */
export function useStale(q: { isError: boolean; dataUpdatedAt: number }): { stale: boolean; ageSeconds: number } {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 5000)
    return () => window.clearInterval(t)
  }, [])
  const ageSeconds = q.dataUpdatedAt ? Math.max(0, Math.floor((now - q.dataUpdatedAt) / 1000)) : 0
  return { stale: q.isError || ageSeconds > 15, ageSeconds }
}

export function sessionRef(s: Pick<LiveSession, 'integration' | 'key'>): string {
  return `${s.integration}:${s.key}`
}

// ---------------------------------------------------------------- elapsed time
export function useElapsed(session: LiveSession | undefined, fetchedAt: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!session || session.status === 'stopped') return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [session])
  if (!session) return 0
  if (session.status === 'stopped') return session.elapsed_seconds
  return session.elapsed_seconds + Math.max(0, Math.floor((now - fetchedAt) / 1000))
}

export function formatElapsed(total: number): string {
  const s = Math.max(0, Math.floor(total))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(h)}:${pad(m)}:${pad(sec)}`
}

// ---------------------------------------------------------------- frame streaming
export interface FrameMeta {
  width: number
  height: number
  frameId: string
  receivedAt: number
}
export type StreamState = 'loading' | 'live' | 'reconnecting' | 'ended' | 'unavailable'

/**
 * Sequential frame loop: the next request starts only after the previous frame arrived,
 * so a slow connection degrades to a lower frame rate instead of piling up requests.
 * The `live` state is only reported while frames really keep arriving.
 */
export function useFrameStream(opts: {
  integration: string
  sessionKey: string
  quality: 'thumb' | 'full'
  enabled: boolean
}) {
  const { integration, sessionKey, quality, enabled } = opts
  const [src, setSrc] = useState<string | null>(null)
  const [meta, setMeta] = useState<FrameMeta | null>(null)
  const [state, setState] = useState<StreamState>('loading')
  const [fps, setFps] = useState(0)
  const kickRef = useRef<() => void>(() => undefined)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    let timer: number | undefined
    let failures = 0
    let lastUrl: string | null = null
    let lastFrameId = ''
    const stamps: number[] = []
    const ctl = new AbortController()
    const target = quality === 'full' ? 220 : 1500

    const schedule = (ms: number) => {
      if (cancelled) return
      window.clearTimeout(timer)
      timer = window.setTimeout(tick, ms)
    }

    async function tick() {
      if (cancelled) return
      if (document.hidden) return schedule(1000)
      const started = performance.now()
      try {
        const frame = await fetchFrame(integration, sessionKey, quality, ctl.signal)
        if (cancelled) return
        failures = 0
        if (frame.frameId !== lastFrameId) {
          lastFrameId = frame.frameId
          const url = URL.createObjectURL(frame.blob)
          setSrc(url)
          if (lastUrl) URL.revokeObjectURL(lastUrl)
          lastUrl = url
          const now = Date.now()
          setMeta({ width: frame.width, height: frame.height, frameId: frame.frameId, receivedAt: now })
          stamps.push(now)
          while (stamps.length && now - stamps[0] > 5000) stamps.shift()
          setFps(stamps.length > 1 ? Math.round((stamps.length - 1) / ((now - stamps[0]) / 1000) * 10) / 10 : 0)
        }
        setState('live')
      } catch (err) {
        if (cancelled || (err instanceof DOMException && err.name === 'AbortError')) return
        failures += 1
        if (err instanceof ApiError && err.status === 404) {
          setState('ended') // the session is gone: stop asking
          return
        }
        if (err instanceof ApiError && (err.status === 422 || err.status === 403)) {
          setState('unavailable')
          return
        }
        setState('reconnecting')
        return schedule(Math.min(1000 * 2 ** Math.min(failures, 3), 8000))
      }
      schedule(Math.max(0, target - (performance.now() - started)))
    }

    kickRef.current = () => schedule(0)
    setState('loading')
    void tick()
    const onVisible = () => !document.hidden && schedule(0)
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      cancelled = true
      ctl.abort()
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
      if (lastUrl) URL.revokeObjectURL(lastUrl)
      setSrc(null)
    }
  }, [integration, sessionKey, quality, enabled])

  const refresh = useCallback(() => kickRef.current(), [])
  return { src, meta, state, fps, refresh }
}

// ---------------------------------------------------------------- manual control
export interface ControlState {
  /** Interactive control is ON only while we hold a server-issued lock. */
  active: boolean
  mode: 'click' | 'full' | null
  busy: boolean
  note: string | null
}

/**
 * Screen Click Mode. OFF by default. Turning it on acquires the exclusive lock; every
 * input event carries the lock token and a strictly increasing sequence number; turning
 * it off drops queued events locally first and then releases the lock server-side.
 */
export function useSessionControl(integration: string, sessionKey: string, onInputDone?: () => void) {
  const [state, setState] = useState<ControlState>({ active: false, mode: null, busy: false, note: null })
  const lockRef = useRef<{ token: string; mode: 'click' | 'full' } | null>(null)
  const epochRef = useRef(0)
  const seqRef = useRef(0)
  const chainRef = useRef<Promise<void>>(Promise.resolve())
  const noteTimer = useRef<number>()
  const doneRef = useRef(onInputDone)
  doneRef.current = onInputDone

  const note = useCallback((text: string | null, sticky = false) => {
    setState((s) => ({ ...s, note: text }))
    window.clearTimeout(noteTimer.current)
    if (text && !sticky) noteTimer.current = window.setTimeout(() => setState((s) => ({ ...s, note: null })), 3500)
  }, [])

  const dropLocal = useCallback(
    (message: string | null) => {
      epochRef.current += 1 // anything still queued is now stale and will not be sent
      lockRef.current = null
      setState({ active: false, mode: null, busy: false, note: message })
    },
    [],
  )

  const enable = useCallback(
    async (mode: 'click' | 'full') => {
      setState((s) => ({ ...s, busy: true, note: null }))
      const epoch = epochRef.current
      try {
        const grant = await api.liveAcquireControl(integration, sessionKey, mode)
        if (epoch !== epochRef.current) {
          // The view moved to another session (or closed) while the lock was being granted.
          void api.liveReleaseControl(integration, sessionKey, grant.token).catch(() => undefined)
          return
        }
        seqRef.current = Math.max(seqRef.current, grant.last_seq ?? 0)
        lockRef.current = { token: grant.token, mode: grant.mode }
        setState({ active: true, mode: grant.mode, busy: false, note: null })
      } catch (e) {
        setState({ active: false, mode: null, busy: false, note: e instanceof Error ? e.message : 'Could not take control' })
      }
    },
    [integration, sessionKey],
  )

  const disable = useCallback(async () => {
    const lock = lockRef.current
    dropLocal(null) // stop forwarding immediately, before the network round trip
    if (!lock) return
    try {
      await api.liveReleaseControl(integration, sessionKey, lock.token)
    } catch {
      // The lock expires by itself; the token is already unusable on our side.
    }
  }, [dropLocal, integration, sessionKey])

  const send = useCallback(
    (event: InputEvent) => {
      const lock = lockRef.current
      if (!lock) return // view-only: nothing is ever sent
      const epoch = epochRef.current
      chainRef.current = chainRef.current.then(async () => {
        if (epoch !== epochRef.current || lockRef.current?.token !== lock.token) return // stale
        seqRef.current += 1
        try {
          await api.liveInput(integration, sessionKey, lock.token, seqRef.current, event)
          doneRef.current?.()
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) {
            const code = (e.detail as { detail?: { code?: string } } | undefined)?.detail?.code
            if (code === 'no_lock' || code === 'no_session') return dropLocal('Manual control ended (lock lost or expired).')
            if (code === 'stale_frame') doneRef.current?.()
          }
          note(e instanceof Error ? e.message : 'Input failed')
        }
      })
    },
    [dropLocal, integration, note, sessionKey],
  )

  // Heartbeat keeps the lock alive only while this tab is really in control.
  useEffect(() => {
    if (!state.active) return
    const t = window.setInterval(async () => {
      const lock = lockRef.current
      if (!lock) return
      try {
        await api.liveRenewControl(integration, sessionKey, lock.token)
      } catch {
        dropLocal('Manual control ended (lock lost or expired).')
      }
    }, 12000)
    return () => window.clearInterval(t)
  }, [dropLocal, integration, sessionKey, state.active])

  // Never leave a lock behind when the tab or view goes away.
  useEffect(() => {
    const onHide = () => {
      const lock = lockRef.current
      if (lock) releaseControlOnUnload(integration, sessionKey, lock.token)
    }
    window.addEventListener('pagehide', onHide)
    return () => {
      window.removeEventListener('pagehide', onHide)
      onHide()
      epochRef.current += 1
      // This hook instance can be reused for another session: nothing of the old lock may carry over.
      lockRef.current = null
      seqRef.current = 0
      setState({ active: false, mode: null, busy: false, note: null })
    }
  }, [integration, sessionKey])

  return { ...state, enable, disable, send }
}
