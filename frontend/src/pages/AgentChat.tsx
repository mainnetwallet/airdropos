/** AI Agent Console: natural-language chat with the central agent (dark "Agent Chat" screen on mobile). */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useLocation } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Activity, Bot, Info, ListChecks, Plus, Search, Send, ShieldCheck, Trash2, Wallet } from 'lucide-react'
import clsx from 'clsx'
import { api } from '@/services/api'
import { agentChatStore } from '@/services/agentChatStore'
import { Alert, Card } from '@/components/ui'

/** Quick actions only fill the composer; nothing is sent until the operator presses send. */
const QUICK_ACTIONS = [
  { label: 'Find Airdrops', icon: Search, prompt: 'Find new legitimate airdrops and summarize the eligibility requirements.' },
  { label: 'Analyze Project', icon: Activity, prompt: 'Analyze this airdrop project and its risks: ' },
  { label: 'Check Wallets', icon: Wallet, prompt: 'Show the status of my configured wallets.' },
  { label: 'Create Task', icon: ListChecks, prompt: 'Create a task to ' },
]

function clock(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function AgentAvatar() {
  return (
    <span className="mt-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-b from-sky-400 to-accent-hover text-white ring-2 ring-sky-300/30 lg:hidden">
      <Bot className="h-5 w-5" />
    </span>
  )
}

export default function AgentChat() {
  // Conversation lives in a persistent external store, not component state, so
  // it survives navigating to another page and back.
  const { messages, pending } = useSyncExternalStore(
    agentChatStore.subscribe,
    agentChatStore.getSnapshot,
  )
  const location = useLocation()
  // Other screens can hand over a draft (e.g. "define a goal"); it is never auto-sent.
  const [input, setInput] = useState(() => (location.state as { prompt?: string } | null)?.prompt ?? '')
  const [showInfo, setShowInfo] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const { data: info } = useQuery({ queryKey: ['agent-info'], queryFn: api.agentInfo })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, pending])

  const send = useMutation({
    mutationFn: ({
      message,
      history,
    }: {
      message: string
      history: Array<{ role: 'user' | 'assistant'; content: string }>
    }) => api.agentChat(message, history),
    onMutate: () => agentChatStore.setPending(true),
    onSuccess: (response) => {
      agentChatStore.add({
        role: 'agent',
        content: response.reply,
        taskId: response.task?.task_id,
      })
    },
    onError: (error: Error) => {
      agentChatStore.add({
        role: 'agent',
        content: `Request failed: ${error.message}`,
        error: true,
      })
    },
    onSettled: () => agentChatStore.setPending(false),
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || pending) return

    // Everything said so far (before this message) is sent along so the agent
    // remembers the conversation. Failed requests are not part of it.
    const history = agentChatStore
      .getSnapshot()
      .messages.filter((m) => !m.error)
      .map((m) => ({
        role: m.role === 'user' ? ('user' as const) : ('assistant' as const),
        content: m.content,
      }))

    agentChatStore.add({ role: 'user', content: text })
    setInput('')
    send.mutate({ message: text, history })
  }

  const clearChat = () => {
    if (window.confirm('Clear this conversation?')) agentChatStore.clear()
  }

  const fillPrompt = (prompt: string) => {
    setInput(prompt)
    inputRef.current?.focus()
  }

  return (
    <div className="flex h-full flex-col gap-3 px-4 pb-2 pt-1 lg:gap-4 lg:p-0">
      {/* Header: phone */}
      <header className="flex items-center justify-between gap-3 lg:hidden">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-slate-100">Agent Chat</h1>
          <span
            className={clsx(
              'mt-1 inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-semibold',
              info ? 'bg-emerald-500/15 text-emerald-400' : 'bg-slate-500/20 text-slate-400',
            )}
          >
            <span className={clsx('h-1.5 w-1.5 rounded-full', info ? 'bg-emerald-400' : 'bg-slate-400')} />
            {info ? 'Online' : 'Connecting'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowInfo((v) => !v)}
            aria-pressed={showInfo}
            aria-label="Model information"
            className={clsx(
              'flex h-11 w-11 items-center justify-center rounded-xl border border-surface-300 bg-surface-200 text-slate-200',
              showInfo && 'border-accent text-accent',
            )}
          >
            <Info className="h-5 w-5" />
          </button>
          <button
            type="button"
            onClick={clearChat}
            aria-label="Start a new conversation"
            className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent text-white shadow-[0_6px_16px_rgba(37,99,235,0.45)]"
          >
            <Plus className="h-6 w-6" />
          </button>
        </div>
      </header>

      {/* Header: desktop */}
      <header className="hidden items-end justify-between gap-4 lg:flex">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">AI Agent Console</h1>
          <p className="mt-1 text-sm text-slate-400">
            Natural-language control of the central autonomous agent
          </p>
        </div>
        {messages.length > 0 && (
          <button type="button" className="btn-ghost" onClick={clearChat}>
            <Trash2 className="h-4 w-4" />
            Clear chat
          </button>
        )}
      </header>

      {info && (
        <div className={clsx(!showInfo && 'hidden lg:block')}>
          <Alert kind="info" title="Single fixed model">
            All reasoning runs on <span className="font-mono">{info.model_id}</span> at{' '}
            <span className="font-mono">{info.base_url}</span>. There is no model switching, fallback,
            or routing. API key is configured but never returned to the browser.
          </Alert>
        </div>
      )}

      <Card className="flex min-h-0 flex-1 flex-col max-lg:border-0 max-lg:bg-transparent max-lg:p-0 max-lg:shadow-none">
        <div className="flex-1 space-y-4 overflow-y-auto pb-3 lg:space-y-3 lg:pb-4">
          {messages.length === 0 && (
            <>
              {/* Phone: the same greeting the reference opens with (interface text, not stored or sent). */}
              <div className="flex items-start gap-2 lg:hidden">
                <AgentAvatar />
                <div className="max-w-[82%] rounded-2xl rounded-tl-md border border-surface-300 bg-surface-200 px-4 py-3 text-[15px] leading-relaxed text-slate-100">
                  Hello! I’m your AirdropOS agent. I can help you discover airdrops, manage tasks, and
                  automate repetitive work. What would you like me to do?
                </div>
              </div>
              <div className="hidden h-full flex-col items-center justify-center gap-2 text-center lg:flex">
                <ShieldCheck className="h-10 w-10 text-slate-600" />
                <p className="font-medium text-slate-300">Ask the agent to do something</p>
                <p className="max-w-md text-sm text-slate-500">
                  e.g. “Discover legitimate airdrops and research eligibility for my configured
                  wallets”, “Show me the status of AIR-2026-000001”, or “Report today's activity”.
                </p>
              </div>
            </>
          )}

          {messages.map((m) => {
            const mine = m.role === 'user'
            return (
              <div key={m.id} className={clsx('flex items-start gap-2', mine ? 'justify-end' : 'justify-start')}>
                {!mine && <AgentAvatar />}
                <div className={clsx('flex max-w-[82%] flex-col lg:max-w-[80%]', mine ? 'items-end' : 'items-start')}>
                  <div
                    className={clsx(
                      'whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5 text-[15px] leading-relaxed lg:text-sm',
                      mine
                        ? 'rounded-tr-md bg-accent text-white lg:rounded-tr-2xl'
                        : m.error
                          ? 'rounded-tl-md border border-status-error/40 bg-status-error/10 text-slate-100 lg:rounded-tl-2xl'
                          : 'rounded-tl-md border border-surface-300 bg-surface-200 text-slate-100 lg:rounded-tl-2xl lg:border-0 lg:text-slate-200',
                    )}
                  >
                    {m.content}
                    {m.taskId && (
                      <div className="mt-2 inline-block rounded-md bg-emerald-500/15 px-2 py-1 font-mono text-xs text-emerald-300">
                        {m.taskId}
                      </div>
                    )}
                  </div>
                  <span className="mt-1 px-1 text-[11px] text-slate-500 lg:hidden">{clock(m.at)}</span>
                </div>
              </div>
            )
          })}

          {pending && (
            <div className="flex items-start gap-2">
              <AgentAvatar />
              <div className="rounded-2xl rounded-tl-md border border-surface-300 bg-surface-200 px-4 py-2.5 lg:border-0">
                <span className="animate-pulse text-sm text-slate-400">Thinking…</span>
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Quick actions (phone) */}
        <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4 pb-2 lg:hidden">
          {QUICK_ACTIONS.map(({ label, icon: Icon, prompt }) => (
            <button
              key={label}
              type="button"
              onClick={() => fillPrompt(prompt)}
              className="flex shrink-0 items-center gap-2 rounded-xl border border-accent/40 bg-accent/10 px-3 py-2 text-[13px] font-medium text-sky-300"
            >
              <Icon className="h-4 w-4" />
              {label}
            </button>
          ))}
        </div>

        <form
          onSubmit={handleSubmit}
          className="flex items-center gap-2 lg:border-t lg:border-surface-300 lg:pt-3"
        >
          <input
            ref={inputRef}
            className="min-w-0 flex-1 rounded-full border border-surface-300 bg-surface-200 px-4 py-3 text-base text-slate-100 placeholder-slate-500 focus:border-accent focus:outline-none lg:rounded-lg lg:py-2 lg:text-sm"
            placeholder="Type a message…"
            aria-label="Message to the agent"
            enterKeyHint="send"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={pending}
          />
          <button
            type="submit"
            aria-label="Send"
            className="btn-primary h-12 w-12 shrink-0 justify-center rounded-full p-0 shadow-[0_6px_16px_rgba(37,99,235,0.45)] lg:h-auto lg:w-auto lg:rounded-lg lg:px-4 lg:py-2 lg:shadow-none"
            disabled={!input.trim() || pending}
          >
            <Send className="h-5 w-5 lg:h-4 lg:w-4" />
            <span className="hidden lg:inline">Send</span>
          </button>
        </form>
      </Card>
    </div>
  )
}
