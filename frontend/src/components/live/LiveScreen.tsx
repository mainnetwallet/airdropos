/**
 * The live picture of a session, plus (only when `interactive`) accurate input mapping.
 *
 * Coordinate mapping: the image fills an overlay that has EXACTLY the aspect ratio of the
 * real viewport, so a pointer position maps to the remote page as a fraction of the overlay's
 * bounding box. Fractions do not depend on preview size, fullscreen, CSS scaling or device
 * pixel ratio; the worker multiplies them by the real viewport size.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'
import { Loader2, WifiOff } from 'lucide-react'
import clsx from 'clsx'
import { useFrameStream, type FrameMeta, type StreamState } from '@/hooks/useLive'
import type { InputEvent } from '@/types/live'

const TAP_MAX_MOVE = 8 // px
const TAP_MAX_MS = 700
const SCROLL_THROTTLE_MS = 60

interface Ripple {
  id: number
  x: number
  y: number
}

export interface LiveScreenProps {
  integration: string
  sessionKey: string
  quality: 'thumb' | 'full'
  /** Fetch frames only while this is true (offscreen cards pause). */
  enabled?: boolean
  /** When true, taps/clicks/scroll are forwarded through `onInput`. Otherwise the view is inert. */
  interactive?: boolean
  onInput?: (event: InputEvent) => void
  /** `contain`: fit inside the parent (expanded view). `width`: full width, natural height (cards). */
  fit?: 'contain' | 'width'
  fallbackSize?: { width: number; height: number }
  onState?: (state: StreamState, fps: number, meta: FrameMeta | null) => void
  onFrameRefreshRequested?: (refresh: () => void) => void
  className?: string
  children?: ReactNode
}

export default function LiveScreen({
  integration,
  sessionKey,
  quality,
  enabled = true,
  interactive = false,
  onInput,
  fit = 'width',
  fallbackSize = { width: 1280, height: 800 },
  onState,
  onFrameRefreshRequested,
  className,
  children,
}: LiveScreenProps) {
  const { src, meta, state, fps, refresh } = useFrameStream({ integration, sessionKey, quality, enabled })
  const hostRef = useRef<HTMLDivElement>(null)
  const surfaceRef = useRef<HTMLDivElement>(null)
  const [box, setBox] = useState<{ w: number; h: number } | null>(null)
  const [ripples, setRipples] = useState<Ripple[]>([])
  const rippleId = useRef(0)
  const gesture = useRef<{ id: number; x: number; y: number; t: number; moved: boolean; lastY: number; lastSend: number } | null>(null)
  const metaRef = useRef(meta)
  metaRef.current = meta

  const size = meta ?? fallbackSize
  const ratio = size.width / size.height

  useEffect(() => onState?.(state, fps, meta), [state, fps, meta, onState])
  useEffect(() => onFrameRefreshRequested?.(refresh), [refresh, onFrameRefreshRequested])

  // 'contain' mode: the largest box with the viewport's aspect ratio that fits the host.
  useLayoutEffect(() => {
    if (fit !== 'contain') return
    const el = hostRef.current
    if (!el) return
    const measure = () => {
      const { width, height } = el.getBoundingClientRect()
      if (!width || !height) return
      const w = Math.min(width, height * ratio)
      setBox({ w: Math.floor(w), h: Math.floor(w / ratio) })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [fit, ratio])

  const toFractions = useCallback((clientX: number, clientY: number) => {
    const rect = surfaceRef.current?.getBoundingClientRect()
    if (!rect || !rect.width || !rect.height) return null
    const clamp = (v: number) => Math.min(1, Math.max(0, v))
    return { fx: clamp((clientX - rect.left) / rect.width), fy: clamp((clientY - rect.top) / rect.height), rect }
  }, [])

  const frameDims = () => {
    const m = metaRef.current
    return m ? { frame_w: m.width, frame_h: m.height } : {}
  }

  const addRipple = (x: number, y: number) => {
    const id = ++rippleId.current
    setRipples((r) => [...r.slice(-3), { id, x, y }])
    window.setTimeout(() => setRipples((r) => r.filter((q) => q.id !== id)), 600)
  }

  // Pointer gestures: a short, still press is a click. A dragging touch scrolls the remote page.
  const onPointerDown = (e: ReactPointerEvent) => {
    if (!interactive || (e.pointerType === 'mouse' && e.button !== 0)) return
    e.currentTarget.setPointerCapture(e.pointerId)
    gesture.current = { id: e.pointerId, x: e.clientX, y: e.clientY, t: performance.now(), moved: false, lastY: e.clientY, lastSend: 0 }
  }
  const onPointerMove = (e: ReactPointerEvent) => {
    const g = gesture.current
    if (!interactive || !g || g.id !== e.pointerId) return
    if (Math.hypot(e.clientX - g.x, e.clientY - g.y) > TAP_MAX_MOVE) g.moved = true
    if (g.moved && e.pointerType !== 'mouse') {
      const now = performance.now()
      if (now - g.lastSend < SCROLL_THROTTLE_MS) return
      const pos = toFractions(e.clientX, e.clientY)
      if (!pos) return
      const scale = size.height / pos.rect.height // screen px -> remote CSS px
      const dy = (g.lastY - e.clientY) * scale
      g.lastY = e.clientY
      g.lastSend = now
      if (Math.abs(dy) >= 1) onInput?.({ type: 'wheel', fx: pos.fx, fy: pos.fy, dx: 0, dy, ...frameDims() })
    }
  }
  const onPointerUp = (e: ReactPointerEvent) => {
    const g = gesture.current
    gesture.current = null
    if (!interactive || !g || g.id !== e.pointerId) return
    if (g.moved || performance.now() - g.t > TAP_MAX_MS) return
    const pos = toFractions(e.clientX, e.clientY)
    if (!pos) return
    addRipple(e.clientX - pos.rect.left, e.clientY - pos.rect.top)
    onInput?.({ type: 'click', fx: pos.fx, fy: pos.fy, ...frameDims() })
  }
  const onPointerCancel = () => {
    gesture.current = null
  }

  // Wheel needs a non-passive native listener so the dashboard itself does not scroll.
  useEffect(() => {
    const el = surfaceRef.current
    if (!el || !interactive) return
    const handler = (e: WheelEvent) => {
      e.preventDefault()
      const pos = toFractions(e.clientX, e.clientY)
      if (!pos) return
      const scale = size.height / pos.rect.height
      const unit = e.deltaMode === 1 ? 16 : 1
      onInput?.({ type: 'wheel', fx: pos.fx, fy: pos.fy, dx: e.deltaX * unit * scale, dy: e.deltaY * unit * scale, ...frameDims() })
    }
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, onInput, size.height, toFractions])

  const surfaceStyle: CSSProperties =
    fit === 'contain'
      ? { width: box?.w ?? 0, height: box?.h ?? 0 }
      : { width: '100%', aspectRatio: `${size.width} / ${size.height}` }

  return (
    <div
      ref={hostRef}
      className={clsx(fit === 'contain' ? 'flex h-full w-full items-center justify-center' : 'w-full', className)}
    >
      <div
        ref={surfaceRef}
        style={surfaceStyle}
        className={clsx(
          'relative overflow-hidden rounded-lg bg-slate-900/90',
          interactive ? 'cursor-crosshair ring-2 ring-accent' : '',
        )}
      >
        {src && (
          <img
            src={src}
            alt={`Live screen of ${sessionKey}`}
            draggable={false}
            className="absolute inset-0 h-full w-full select-none object-fill"
          />
        )}

        {/* Input layer: inert unless interactive, so ordinary taps and scrolls never reach the session. */}
        <div
          className={clsx('absolute inset-0', interactive ? 'pointer-events-auto' : 'pointer-events-none')}
          style={interactive ? { touchAction: 'none' } : undefined}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerCancel}
          onContextMenu={(e) => interactive && e.preventDefault()}
        >
          {ripples.map((r) => (
            <span
              key={r.id}
              className="pointer-events-none absolute h-6 w-6 -translate-x-1/2 -translate-y-1/2 animate-ping rounded-full bg-accent/60"
              style={{ left: r.x, top: r.y }}
            />
          ))}
        </div>

        {state === 'loading' && !src && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 text-xs text-slate-400">
            <Loader2 className="h-4 w-4 animate-spin" /> Connecting…
          </div>
        )}
        {state === 'reconnecting' && (
          <div className="absolute inset-x-0 top-0 flex items-center justify-center gap-1.5 bg-amber-500/90 px-2 py-1 text-[11px] font-semibold text-white">
            <Loader2 className="h-3 w-3 animate-spin" /> Reconnecting…
          </div>
        )}
        {(state === 'ended' || state === 'unavailable') && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-slate-900/80 p-3 text-center text-xs text-slate-300">
            <WifiOff className="h-5 w-5" />
            {state === 'ended' ? 'Session ended or not running' : 'Live screen not available'}
          </div>
        )}
        {children}
      </div>
    </div>
  )
}
