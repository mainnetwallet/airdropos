/**
 * A real-Telegram-like view of the logged-in Telegram account, inside Live Broadcast.
 *
 * Chat list on the left, the conversation on the right (a single column on a phone). Inline
 * bot buttons can be pressed and messages can be sent, exactly like the Telegram app. Every
 * piece of data here comes from the live Telethon session on the worker; nothing is invented.
 * Pressing and sending need the same permission as any other Live Broadcast control.
 */

import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Bot, Download, ExternalLink, FileText, Film, Globe, Loader2, Lock, Megaphone, Music, Play, Search, SendHorizontal, Users, X } from 'lucide-react'
import clsx from 'clsx'
import { fetchChatFile, fetchChatPreview, liveApi as api } from '@/services/api'
import Modal from '@/components/live/Modal'
import type { ChatButton, ChatDialog, ChatMediaInfo, ChatMessage, LiveSession } from '@/types/live'

const AVATAR_COLORS = ['#e17076', '#7bc862', '#e5ca77', '#65aadd', '#a695e7', '#ee7aae', '#6ec9cb', '#faa774']

function avatarColor(seed: string): string {
  let h = 0
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0
  return AVATAR_COLORS[h % AVATAR_COLORS.length]
}

function Avatar({ title, kind, size = 44 }: { title: string; kind?: string | null; size?: number }) {
  const initial = (title.trim()[0] ?? '?').toUpperCase()
  return (
    <span
      className="flex shrink-0 items-center justify-center rounded-full font-semibold text-white"
      style={{ width: size, height: size, background: avatarColor(title), fontSize: size * 0.42 }}
      aria-hidden
    >
      {kind === 'bot' ? <Bot style={{ width: size * 0.5, height: size * 0.5 }} /> : initial}
    </span>
  )
}

function clock(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

const URL_RE = /(https?:\/\/[^\s<>"']+)/g

/** Plain text with http(s) links made clickable. Nothing is ever injected as HTML. */
function Linkified({ text }: { text: string }) {
  const parts = text.split(URL_RE)
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <a key={i} href={part} target="_blank" rel="noopener noreferrer" className="text-[#6ab3f3] underline break-all">
            {part}
          </a>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        ),
      )}
    </>
  )
}

function DialogKindIcon({ kind }: { kind: string | null }) {
  if (kind === 'channel') return <Megaphone className="h-3.5 w-3.5 shrink-0 text-slate-400" />
  if (kind === 'group' || kind === 'supergroup') return <Users className="h-3.5 w-3.5 shrink-0 text-slate-400" />
  if (kind === 'bot') return <Bot className="h-3.5 w-3.5 shrink-0 text-slate-400" />
  return null
}

function fmtSize(n: number | null): string {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function saveBlob(blob: Blob, name: string) {
  // A download, never a navigation: the file is saved, not rendered inside the dashboard.
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

function useBlobUrl(blob: Blob | undefined): string | null {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!blob) {
      setUrl(null)
      return
    }
    const u = URL.createObjectURL(blob)
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [blob])
  return url
}

function KindIcon({ kind }: { kind: ChatMediaInfo['kind'] }) {
  if (kind === 'video' || kind === 'gif') return <Film className="h-5 w-5" />
  if (kind === 'audio' || kind === 'voice') return <Music className="h-5 w-5" />
  return <FileText className="h-5 w-5" />
}

function MediaBlock({
  info,
  messageId,
  integration,
  sessionKey,
  peer,
  canWrite,
}: {
  info: ChatMediaInfo
  messageId: number
  integration: string
  sessionKey: string
  peer: string
  canWrite: boolean
}) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const preview = useQuery({
    queryKey: ['tg-media', integration, sessionKey, peer, messageId],
    queryFn: () => fetchChatPreview(integration, sessionKey, peer, messageId),
    enabled: info.preview,
    staleTime: Infinity,
    gcTime: 10 * 60_000,
    retry: false,
  })
  const src = useBlobUrl(preview.data)
  const download = useMutation({
    mutationFn: () => fetchChatFile(integration, sessionKey, peer, messageId),
    onSuccess: (blob) => {
      setError(null)
      saveBlob(blob, info.name)
    },
    onError: (e) => setError(e instanceof Error ? e.message : 'Download failed.'),
  })
  const canDownload = canWrite && info.downloadable && info.kind !== 'voice'
  const downloadBtn = (
    <button
      type="button"
      disabled={!canDownload || download.isPending}
      onClick={() => download.mutate()}
      title={!canWrite ? 'Downloading needs control permission' : !info.downloadable ? 'Too large to download here' : 'Download'}
      className="flex items-center gap-1 rounded-full bg-black/30 px-2.5 py-1 text-xs font-medium text-white hover:bg-black/50 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {download.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
      Download
    </button>
  )

  const showImage = info.preview && !!src
  const stillLoading = info.preview && preview.isLoading
  return (
    <div className="mb-1">
      {showImage ? (
        <button type="button" onClick={() => setOpen(true)} className="relative block overflow-hidden rounded-lg" aria-label={`Open ${info.name}`}>
          <img src={src as string} alt={info.name} className="max-h-64 w-full max-w-[320px] object-cover" />
          {(info.kind === 'video' || info.kind === 'gif') && (
            <span className="absolute inset-0 flex items-center justify-center bg-black/25">
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-black/55">
                <Play className="h-5 w-5 text-white" />
              </span>
            </span>
          )}
        </button>
      ) : stillLoading ? (
        <div className="flex h-32 w-52 items-center justify-center rounded-lg bg-black/25 text-slate-400">
          <Loader2 className="h-5 w-5 animate-spin" />
        </div>
      ) : (
        <div className="flex items-center gap-3 rounded-lg bg-black/25 p-2">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-[#3b9bdc] text-white">
            <KindIcon kind={info.kind} />
          </span>
          <span className="min-w-0">
            <span className="block truncate text-sm font-medium">{info.name}</span>
            <span className="block text-xs text-slate-400">
              {fmtSize(info.size)}
              {!info.downloadable ? ' · too large to download here' : ''}
            </span>
          </span>
        </div>
      )}
      <div className="mt-1 flex items-center gap-2">
        {(showImage || info.kind !== 'voice') && downloadBtn}
        {showImage && info.size != null && <span className="text-[11px] text-slate-400">{info.name} · {fmtSize(info.size)}</span>}
      </div>
      {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
      {open && showImage && (
        <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/85 p-4" onMouseDown={() => setOpen(false)}>
          <button type="button" aria-label="Close" onClick={() => setOpen(false)} className="absolute right-4 top-4 rounded-full bg-white/10 p-2 text-white hover:bg-white/20">
            <X className="h-5 w-5" />
          </button>
          <img src={src as string} alt={info.name} onMouseDown={(e) => e.stopPropagation()} className="max-h-[88vh] max-w-full rounded-lg object-contain" />
        </div>
      )}
    </div>
  )
}

function MiniAppModal({
  buttonText,
  busy,
  error,
  onClose,
  onOpen,
}: {
  buttonText: string
  busy: boolean
  error: string | null
  onClose: () => void
  onOpen: (profileId: string) => void
}) {
  const profiles = useQuery({ queryKey: ['browser-profiles'], queryFn: api.browserProfiles, retry: false })
  const list = profiles.data?.profiles ?? []
  const [choice, setChoice] = useState('')
  const selected = choice || list[0]?.id || ''
  return (
    <Modal title="Open Mini App in a browser" onClose={onClose}>
      <p className="mb-3 text-sm text-slate-400">
        "{buttonText}" opens in a browser session you can watch and click in Live Broadcast. Telegram signs the launch for this account, and
        the link is passed straight to the browser, never shown here. Only open Mini Apps you trust: they run in the chosen profile.
      </p>
      {profiles.isLoading ? (
        <p className="text-sm text-slate-500">Loading profiles…</p>
      ) : list.length === 0 ? (
        <p className="rounded-lg bg-surface-200 p-3 text-sm text-slate-400">Create a browser profile first (Browser Profiles page), then try again.</p>
      ) : (
        <div className="space-y-3">
          <label className="block text-xs font-medium text-slate-400">
            Browser profile
            <select className="input mt-1 w-full" value={selected} onChange={(e) => setChoice(e.target.value)}>
              {list.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          {error && <p className="text-sm text-status-error">{error}</p>}
          <button type="button" disabled={busy || !selected} onClick={() => onOpen(selected)} className="btn btn-primary w-full">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Globe className="h-4 w-4" />} Open in browser
          </button>
        </div>
      )}
    </Modal>
  )
}

function ButtonGrid({
  message,
  disabled,
  pending,
  onPress,
  onMiniApp,
}: {
  message: ChatMessage
  disabled: boolean
  pending: boolean
  onPress: (m: ChatMessage, b: ChatButton) => void
  onMiniApp: (m: ChatMessage, b: ChatButton) => void
}) {
  if (!message.buttons.length) return null
  const base =
    'flex min-h-[34px] flex-1 items-center justify-center gap-1 rounded-lg bg-black/35 px-2 py-1.5 text-center text-[13px] font-medium text-white transition-colors'
  return (
    <div className="mt-1 space-y-1">
      {message.buttons.map((row, r) => (
        <div key={r} className="flex gap-1">
          {row.map((b) =>
            b.kind === 'url' && b.url ? (
              <a key={b.col} href={b.url} target="_blank" rel="noopener noreferrer" className={clsx(base, 'hover:bg-black/55')}>
                <span className="truncate">{b.text}</span>
                <ExternalLink className="h-3 w-3 shrink-0 opacity-70" />
              </a>
            ) : b.miniapp ? (
              <button
                key={b.col}
                type="button"
                disabled={disabled}
                onClick={() => onMiniApp(message, b)}
                title="Open this Mini App in a browser session"
                className={clsx(base, disabled ? 'cursor-not-allowed opacity-50' : 'hover:bg-black/55')}
              >
                <Globe className="h-3 w-3 shrink-0 opacity-70" />
                <span className="truncate">{b.text}</span>
              </button>
            ) : (
              <button
                key={b.col}
                type="button"
                disabled={disabled || pending || !b.clickable}
                onClick={() => onPress(message, b)}
                title={b.clickable ? undefined : 'This button type cannot be pressed from here'}
                className={clsx(base, b.clickable && !disabled ? 'hover:bg-black/55' : 'cursor-not-allowed opacity-50')}
              >
                <span className="truncate">{b.text}</span>
              </button>
            ),
          )}
        </div>
      ))}
    </div>
  )
}

function Bubble({
  m,
  disabled,
  pending,
  onPress,
  onMiniApp,
  media,
}: {
  m: ChatMessage
  disabled: boolean
  pending: boolean
  onPress: (m: ChatMessage, b: ChatButton) => void
  onMiniApp: (m: ChatMessage, b: ChatButton) => void
  media: { integration: string; sessionKey: string; peer: string; canWrite: boolean }
}) {
  return (
    <div className={clsx('flex', m.from_me ? 'justify-end' : 'justify-start')}>
      <div className={clsx('max-w-[85%] sm:max-w-[70%]', m.buttons.length && 'w-full sm:w-auto sm:min-w-[260px]')}>
        <div className={clsx('rounded-2xl px-3 py-1.5 text-sm text-white', m.from_me ? 'rounded-br-md bg-[#2b5278]' : 'rounded-bl-md bg-[#182533]')}>
          {!m.from_me && m.sender && <p className="text-xs font-semibold" style={{ color: avatarColor(m.sender) }}>{m.sender}</p>}
          {m.media_info ? (
            <MediaBlock info={m.media_info} messageId={m.id} {...media} />
          ) : (
            m.media && <p className="text-xs font-medium text-[#6ab3f3]">{m.media}</p>
          )}
          {m.text && (
            <p className="whitespace-pre-wrap break-words">
              <Linkified text={m.text} />
            </p>
          )}
          <p className="mt-0.5 text-right text-[10px] text-slate-400">
            {m.edited ? 'edited · ' : ''}
            {clock(m.date)}
          </p>
        </div>
        <ButtonGrid message={m} disabled={disabled} pending={pending} onPress={onPress} onMiniApp={onMiniApp} />
      </div>
    </div>
  )
}

export default function TelegramChat({
  integration,
  sessionKey,
  stopped,
  canWrite,
  onOpenSession,
  clickEnabled = true,
  compact = false,
  active = true,
}: {
  integration: string
  sessionKey: string
  stopped: boolean
  canWrite: boolean
  onOpenSession?: (session: LiveSession) => void
  /** Click mode. While off, chats can be browsed but nothing is sent, pressed, opened or downloaded. */
  clickEnabled?: boolean
  /** Card preview: the same two-pane UI as the full view (no chat open until one is picked), at a fixed wide layout the card scales down. */
  compact?: boolean
  /** Poll only while the view is on screen. */
  active?: boolean
}) {
  const qc = useQueryClient()
  const [peer, setPeer] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [text, setText] = useState('')
  const [notice, setNotice] = useState<{ kind: 'info' | 'error'; text: string } | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const dialogs = useQuery({
    queryKey: ['tg-dialogs', integration, sessionKey],
    queryFn: () => api.liveChatDialogs(integration, sessionKey),
    enabled: !stopped && active,
    refetchInterval: 8000,
    retry: false,
  })
  const history = useQuery({
    queryKey: ['tg-history', integration, sessionKey, peer],
    queryFn: () => api.liveChatHistory(integration, sessionKey, peer as string),
    enabled: !stopped && active && !!peer,
    refetchInterval: 3000,
    placeholderData: (prev) => prev,
    retry: false,
  })

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['tg-history', integration, sessionKey, peer] })
    qc.invalidateQueries({ queryKey: ['tg-dialogs', integration, sessionKey] })
  }
  const fail = (e: unknown) => setNotice({ kind: 'error', text: e instanceof Error ? e.message : 'Something went wrong.' })

  const send = useMutation({
    mutationFn: (t: string) => api.liveChatSend(integration, sessionKey, peer as string, t),
    onSuccess: () => {
      setText('')
      setNotice(null)
      refresh()
    },
    onError: fail,
  })
  const press = useMutation({
    mutationFn: ({ m, b }: { m: ChatMessage; b: ChatButton }) =>
      api.liveChatClick(integration, sessionKey, peer as string, m.id, b.row, b.col),
    onSuccess: (r) => {
      setNotice(r.answer ? { kind: 'info', text: r.answer } : null)
      refresh()
    },
    onError: fail,
  })

  const reconnect = useMutation({
    mutationFn: () => api.liveResume(integration, sessionKey),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['live'] })
      void qc.invalidateQueries({ queryKey: ['tg-dialogs', integration, sessionKey] })
    },
  })
  const [mini, setMini] = useState<{ m: ChatMessage; b: ChatButton } | null>(null)
  const openMini = useMutation({
    mutationFn: (profileId: string) =>
      api.liveChatMiniApp(integration, sessionKey, peer as string, mini!.m.id, mini!.b.row, mini!.b.col, profileId),
    onSuccess: ({ session }) => {
      setMini(null)
      void qc.invalidateQueries({ queryKey: ['live'] })
      onOpenSession?.(session)
    },
  })

  const messages = history.data?.messages ?? []
  const lastId = messages.length ? messages[messages.length - 1].id : 0
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [lastId, peer])

  const list = useMemo(() => {
    const q = search.trim().toLowerCase()
    const all = dialogs.data?.dialogs ?? []
    return q ? all.filter((d) => `${d.title} ${d.username ?? ''}`.toLowerCase().includes(q)) : all
  }, [dialogs.data, search])

  const readOnly = stopped || !canWrite || !clickEnabled
  const submit = () => {
    const t = text.trim()
    if (!t || readOnly || send.isPending || !peer) return
    send.mutate(t)
  }

  if (stopped) {
    return (
      <div className="flex h-full min-h-[240px] flex-col items-center justify-center gap-3 rounded-xl bg-[#0e1621] p-6 text-center text-sm text-slate-400">
        <p>This Telegram account was stopped, so it is disconnected.</p>
        <button
          type="button"
          disabled={!canWrite || reconnect.isPending}
          onClick={() => reconnect.mutate()}
          className="flex items-center gap-2 rounded-full bg-[#3b9bdc] px-5 py-2 text-sm font-semibold text-white hover:bg-[#2f8acb] disabled:opacity-50"
        >
          {reconnect.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
          Reconnect and open chats
        </button>
        {!canWrite && <p className="text-xs text-slate-500">This login is read-only, so it cannot reconnect.</p>}
        {reconnect.error instanceof Error && <p className="text-xs text-red-300">{reconnect.error.message}</p>}
      </div>
    )
  }

  return (
    <div className={clsx('flex h-full overflow-hidden rounded-xl border border-white/5 bg-[#0e1621] text-slate-100', !compact && 'min-h-[420px]')}>
      {/* ---------------------------------------------------------- chat list */}
      <aside className={clsx(
          'shrink-0 flex-col border-r border-white/5 bg-[#17212b]',
          compact ? 'flex w-72' : clsx('w-full md:flex md:w-72', peer ? 'hidden' : 'flex'),
        )}>
        <div className="p-2">
          <div className="flex items-center gap-2 rounded-full bg-[#242f3d] px-3 py-2">
            <Search className="h-4 w-4 text-slate-400" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search"
              className="w-full bg-transparent text-sm outline-none placeholder:text-slate-500"
            />
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {dialogs.isLoading && (
            <div className="flex items-center justify-center gap-2 p-6 text-sm text-slate-400">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading chats…
            </div>
          )}
          {dialogs.isError && (
            <p className="m-3 rounded-lg bg-red-500/10 p-3 text-xs text-red-300">
              {dialogs.error instanceof Error ? dialogs.error.message : 'Could not load chats.'}
            </p>
          )}
          {!dialogs.isLoading && !dialogs.isError && list.length === 0 && (
            <p className="p-6 text-center text-sm text-slate-500">No chats found.</p>
          )}
          {list.map((d: ChatDialog) => (
            <button
              key={d.peer}
              type="button"
              onClick={() => {
                setPeer(d.peer)
                setNotice(null)
              }}
              className={clsx('flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-white/5', peer === d.peer && 'bg-[#2b5278]/60')}
            >
              <Avatar title={d.title} kind={d.kind} />
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1.5">
                  <DialogKindIcon kind={d.kind} />
                  <span className="truncate text-sm font-semibold">{d.title}</span>
                  <span className="ml-auto shrink-0 text-[11px] text-slate-500">{clock(d.last_date)}</span>
                </span>
                <span className="flex items-center gap-2">
                  <span className="truncate text-xs text-slate-400">
                    {d.last_from_me ? 'You: ' : ''}
                    {d.last_text}
                  </span>
                  {d.unread > 0 && (
                    <span className="ml-auto shrink-0 rounded-full bg-[#3b9bdc] px-1.5 text-[11px] font-semibold text-white">{d.unread}</span>
                  )}
                </span>
              </span>
            </button>
          ))}
        </div>
      </aside>

      {/* ---------------------------------------------------------- conversation */}
      <section className={clsx('min-w-0 flex-1 flex-col', compact ? 'flex' : clsx('md:flex', peer ? 'flex' : 'hidden'))}>
        {!peer ? (
          <div className="flex h-full items-center justify-center p-6 text-center text-sm text-slate-500">Select a chat to start</div>
        ) : (
          <>
            <header className="flex items-center gap-3 border-b border-white/5 bg-[#17212b] px-3 py-2">
              <button type="button" onClick={() => setPeer(null)} aria-label="Back to chats" className="rounded-full p-1.5 hover:bg-white/10 md:hidden">
                <ArrowLeft className="h-5 w-5" />
              </button>
              <Avatar title={history.data?.title ?? '…'} kind={history.data?.is_bot ? 'bot' : null} size={36} />
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold">{history.data?.title ?? 'Loading…'}</p>
                <p className="truncate text-xs text-slate-400">{history.data?.is_bot ? 'bot' : history.data?.username ? `@${history.data.username}` : ''}</p>
              </div>
            </header>

            <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 py-3">
              {history.isLoading && (
                <div className="flex justify-center p-6 text-slate-400">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </div>
              )}
              {history.isError && (
                <p className="rounded-lg bg-red-500/10 p-3 text-xs text-red-300">
                  {history.error instanceof Error ? history.error.message : 'Could not load messages.'}
                </p>
              )}
              {messages.map((m) => (
                <Bubble
                  key={m.id}
                  m={m}
                  disabled={readOnly}
                  pending={press.isPending}
                  onPress={(msg, b) => press.mutate({ m: msg, b })}
                  onMiniApp={(msg, b) => {
                    openMini.reset()
                    setMini({ m: msg, b })
                  }}
                  media={{ integration, sessionKey, peer, canWrite: !readOnly }}
                />
              ))}
              <div ref={bottomRef} />
            </div>

            {notice && (
              <p className={clsx('mx-3 mb-1 rounded-lg px-3 py-1.5 text-xs', notice.kind === 'error' ? 'bg-red-500/10 text-red-300' : 'bg-white/10 text-slate-200')}>
                {notice.text}
              </p>
            )}

            <footer className="flex items-end gap-2 border-t border-white/5 bg-[#17212b] p-2">
              {readOnly ? (
                <p className="flex flex-1 items-center gap-2 px-2 py-2 text-xs text-slate-500">
                  <Lock className="h-3.5 w-3.5" />
                  {canWrite
                    ? 'Click is off. Press Enable Click to send messages, press buttons or download files.'
                    : 'This login is read-only, so messages cannot be sent.'}
                </p>
              ) : (
                <>
                  <textarea
                    value={text}
                    rows={1}
                    maxLength={4096}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault()
                        submit()
                      }
                    }}
                    placeholder="Write a message…"
                    className="max-h-32 min-h-[40px] flex-1 resize-none rounded-2xl bg-[#242f3d] px-3 py-2 text-sm outline-none placeholder:text-slate-500"
                  />
                  <button
                    type="button"
                    onClick={submit}
                    disabled={!text.trim() || send.isPending}
                    aria-label="Send"
                    className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-[#3b9bdc] text-white disabled:opacity-40"
                  >
                    {send.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <SendHorizontal className="h-4 w-4" />}
                  </button>
                </>
              )}
            </footer>
          </>
        )}
      </section>
      {mini && (
        <MiniAppModal
          buttonText={mini.b.text}
          busy={openMini.isPending}
          error={openMini.error instanceof Error ? openMini.error.message : null}
          onClose={() => setMini(null)}
          onOpen={(id) => openMini.mutate(id)}
        />
      )}
    </div>
  )
}
