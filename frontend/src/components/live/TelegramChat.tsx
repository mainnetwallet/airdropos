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
import { ArrowLeft, Bot, ExternalLink, Loader2, Lock, Megaphone, Search, SendHorizontal, Users } from 'lucide-react'
import clsx from 'clsx'
import { liveApi as api } from '@/services/api'
import type { ChatButton, ChatDialog, ChatMessage } from '@/types/live'

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

function ButtonGrid({
  message,
  disabled,
  pending,
  onPress,
}: {
  message: ChatMessage
  disabled: boolean
  pending: boolean
  onPress: (m: ChatMessage, b: ChatButton) => void
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
}: {
  m: ChatMessage
  disabled: boolean
  pending: boolean
  onPress: (m: ChatMessage, b: ChatButton) => void
}) {
  return (
    <div className={clsx('flex', m.from_me ? 'justify-end' : 'justify-start')}>
      <div className={clsx('max-w-[85%] sm:max-w-[70%]', m.buttons.length && 'w-full sm:w-auto sm:min-w-[260px]')}>
        <div className={clsx('rounded-2xl px-3 py-1.5 text-sm text-white', m.from_me ? 'rounded-br-md bg-[#2b5278]' : 'rounded-bl-md bg-[#182533]')}>
          {!m.from_me && m.sender && <p className="text-xs font-semibold" style={{ color: avatarColor(m.sender) }}>{m.sender}</p>}
          {m.media && <p className="text-xs font-medium text-[#6ab3f3]">{m.media}</p>}
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
        <ButtonGrid message={m} disabled={disabled} pending={pending} onPress={onPress} />
      </div>
    </div>
  )
}

export default function TelegramChat({
  integration,
  sessionKey,
  stopped,
  canWrite,
}: {
  integration: string
  sessionKey: string
  stopped: boolean
  canWrite: boolean
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
    enabled: !stopped,
    refetchInterval: 8000,
    retry: false,
  })
  const history = useQuery({
    queryKey: ['tg-history', integration, sessionKey, peer],
    queryFn: () => api.liveChatHistory(integration, sessionKey, peer as string),
    enabled: !stopped && !!peer,
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

  const readOnly = stopped || !canWrite
  const submit = () => {
    const t = text.trim()
    if (!t || readOnly || send.isPending || !peer) return
    send.mutate(t)
  }

  if (stopped) {
    return (
      <div className="flex h-full min-h-[240px] items-center justify-center rounded-xl bg-[#0e1621] p-6 text-center text-sm text-slate-400">
        This Telegram session is stopped. Resume it to open the chats.
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-[420px] overflow-hidden rounded-xl border border-white/5 bg-[#0e1621] text-slate-100">
      {/* ---------------------------------------------------------- chat list */}
      <aside className={clsx('w-full shrink-0 flex-col border-r border-white/5 bg-[#17212b] md:flex md:w-72', peer ? 'hidden' : 'flex')}>
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
      <section className={clsx('min-w-0 flex-1 flex-col md:flex', peer ? 'flex' : 'hidden')}>
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
                <Bubble key={m.id} m={m} disabled={readOnly} pending={press.isPending} onPress={(msg, b) => press.mutate({ m: msg, b })} />
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
                  <Lock className="h-3.5 w-3.5" /> This login is read-only, so messages cannot be sent.
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
    </div>
  )
}
