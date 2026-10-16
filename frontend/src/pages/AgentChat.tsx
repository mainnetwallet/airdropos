/** AI Agent Console: natural-language chat with the central agent (dark "Agent Chat" screen on mobile). */

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowUp,
  Check,
  Copy,
  Info,
  ListChecks,
  Search,
  Send,
  ShieldAlert,
  Trash2,
  Wallet,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '@/services/api'
import { agentChatStore, type ChatMessage, type LiveTool, type LiveTurn } from '@/services/agentChatStore'
import { Alert } from '@/components/ui'
import type { AgentToolCall, AutonomousRule, AutonomyPolicy } from '@/types'

/** Quick actions only fill the composer; nothing is sent until the operator presses send. */
const QUICK_ACTIONS = [
  { label: 'Find Airdrops', icon: Search, prompt: 'Find new legitimate airdrops and summarize the eligibility requirements.' },
  { label: 'Analyze Project', icon: Activity, prompt: 'Analyze this airdrop project and its risks: ' },
  { label: 'My Address', icon: Wallet, prompt: 'Show my Ethereum wallet address.' },
  { label: 'Balances', icon: Wallet, prompt: 'Show my wallet balances on ethereum.' },
  { label: 'Task Status', icon: ListChecks, prompt: 'Show my recent tasks and their current status.' },
  { label: 'Diagnose', icon: AlertTriangle, prompt: 'Run diagnostics and tell me what is broken.' },
]

function clock(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/**
 * A public address the backend returned, with a copy button.
 *
 * The value is whatever the tool reported. Nothing here derives, formats or guesses
 * an address, so the button cannot copy something other than what the server said.
 */
function CopyableAddress({ address, label }: { address: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')

  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(address)
      } else {
        // Older/insecure contexts have no async clipboard API.
        const field = document.createElement('textarea')
        field.value = address
        field.setAttribute('readonly', '')
        field.style.position = 'fixed'
        field.style.opacity = '0'
        document.body.appendChild(field)
        field.select()
        const ok = document.execCommand('copy')
        document.body.removeChild(field)
        if (!ok) throw new Error('copy command rejected')
      }
      setState('copied')
    } catch {
      // Say so. Pretending the copy worked is worse than an explicit failure.
      setState('failed')
    }
  }

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-surface-300 bg-slate-900/60 px-2.5 py-2">
      <code className="min-w-0 break-all font-mono text-xs text-emerald-300">{address}</code>
      <button
        type="button"
        onClick={copy}
        aria-label={label ? `Copy address ${address}` : `Copy address ${address}`}
        className={clsx(
          'flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs font-medium',
          state === 'copied'
            ? 'bg-emerald-500/20 text-emerald-300'
            : state === 'failed'
              ? 'bg-status-error/20 text-status-error'
              : 'bg-surface-300 text-slate-200',
        )}
      >
        {state === 'copied' ? (
          <>
            <Check className="h-3 w-3" /> Copied
          </>
        ) : state === 'failed' ? (
          <>
            <XCircle className="h-3 w-3" /> Copy failed
          </>
        ) : (
          <>
            <Copy className="h-3 w-3" /> Copy
          </>
        )}
      </button>
    </div>
  )
}

/**
 * The one public address a tool result carries, if it has one.
 *
 * Wallet tools return both `address` and `copyable_address` with the same value.
 * Rendering both would show the operator the same string twice, so exactly one is
 * chosen here - and only if the two agree. A disagreement means the result is
 * inconsistent, and showing either one would be picking a winner.
 */
function addressOf(data: Record<string, unknown>): string | null {
  const direct = typeof data.address === 'string' ? data.address : null
  const copyable = typeof data.copyable_address === 'string' ? data.copyable_address : null
  if (direct && copyable && direct !== copyable) return null
  return direct ?? copyable
}

const TOOL_STATUS_STYLE: Record<AgentToolCall['status'], string> = {
  success: 'text-emerald-300',
  failure: 'text-status-error',
  needs_user_action: 'text-amber-300',
  not_authorized: 'text-amber-300',
  unavailable: 'text-slate-400',
}

/**
 * The tools the agent actually ran, with their real outcomes.
 *
 * A failure, a refusal and an "unavailable" are rendered as themselves. The component
 * has no branch that turns any non-success into a tick.
 */
function ToolCallList({ calls }: { calls: AgentToolCall[] }) {
  if (calls.length === 0) return null
  return (
    <div className="mt-2 space-y-1.5">
      {calls.map((call, i) => (
        <div key={`${call.tool}-${i}`} className="rounded-lg border border-surface-300 bg-slate-900/40 px-2.5 py-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate font-mono text-xs text-slate-300">{call.tool}</span>
            <span className={clsx('shrink-0 font-mono text-[11px]', TOOL_STATUS_STYLE[call.status])}>
              {call.status}
            </span>
          </div>
          {call.error && (
            <p className="mt-1 break-words text-xs text-slate-400">{call.error}</p>
          )}
          {call.evidence?.kind && call.status === 'success' && (
            <p className="mt-1 text-[11px] text-slate-500">evidence: {call.evidence.kind}</p>
          )}
          {typeof call.data?.tx_hash === 'string' && call.status === 'success' && (
            // The hash the backend actually returned. Copyable like an address, and
            // never derived in the browser.
            <CopyableAddress address={call.data.tx_hash} label="Copy transaction hash" />
          )}
          {typeof call.data?.status === 'string' && call.status === 'success' && (
            <p className="mt-1 text-[11px] text-slate-400">{call.data.status}</p>
          )}
          {call.status === 'success' && addressOf(call.data) && (
            <CopyableAddress address={addressOf(call.data) as string} />
          )}
        </div>
      ))}
    </div>
  )
}

/** A prepared transfer that still needs the operator's explicit approval. */
function ApprovalNotice({ message }: { message: ChatMessage }) {
  if (!message.approvals?.length) return null
  return (
    <div className="mt-2 space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2.5">
      {message.approvals.map((a) => (
        <div key={a.proposal_id} className="space-y-1.5">
          <p className="flex items-center gap-1.5 text-xs font-semibold text-amber-300">
            <ShieldAlert className="h-3.5 w-3.5" /> Approval required
          </p>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px] text-slate-300">
            <dt className="text-slate-500">Network</dt>
            <dd className="font-mono">{a.network} (chain {a.chain_id})</dd>
            <dt className="text-slate-500">From</dt>
            <dd className="break-all font-mono">{a.wallet}</dd>
            <dt className="text-slate-500">To</dt>
            <dd className="break-all font-mono">{a.to}</dd>
            <dt className="text-slate-500">Amount</dt>
            <dd className="font-mono">
              {a.amount} {a.asset}
            </dd>
            <dt className="text-slate-500">Est. fee</dt>
            <dd className="font-mono">{a.estimated_fee_native}</dd>
          </dl>
          {a.warnings.map((w) => (
            <p key={w} className="text-[11px] text-amber-200/90">
              {w}
            </p>
          ))}
          <p className="text-[11px] text-slate-400">
            Nothing has been signed or sent. Reply to confirm these exact details and the
            agent will record your approval.
          </p>
        </div>
      ))}
    </div>
  )
}

/**
 * What the agent may do unattended, read straight from the backend policy.
 *
 * Defensive on purpose: a display component must never be able to take the console
 * down. An older backend, a proxy error page, or a partial payload renders as
 * "unavailable" rather than throwing and blanking the chat.
 */
function AutonomyPanel({ policy }: { policy: AutonomyPolicy | undefined }) {
  const configured = policy?.policy
  const enabled = Boolean(configured?.enabled)
  const rules = Array.isArray(configured?.rules) ? (configured!.rules as AutonomousRule[]) : []
  const valueMoving = Boolean(configured?.value_moving)
  return (
    <div className="mt-3 rounded-lg border border-surface-300 bg-surface-200/60 px-3 py-2.5 text-xs">
      <p className="flex flex-wrap items-center gap-2 font-semibold text-slate-200">
        <span>Autonomous execution</span>
        <span
          className={clsx(
            'rounded px-1.5 py-0.5 text-[11px]',
            enabled ? 'bg-emerald-500/15 text-emerald-300' : 'bg-slate-500/20 text-slate-400',
          )}
        >
          {enabled ? 'enabled' : 'disabled'}
        </span>
        {valueMoving && (
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[11px] text-amber-300">
            can move funds
          </span>
        )}
      </p>
      <p className="mt-1 text-slate-400">
        {!configured
          ? 'Policy status unavailable.'
          : rules.length === 0
            ? 'No rules configured. Every operation is refused until a rule allows it.'
            : `${rules.length} rule${rules.length === 1 ? '' : 's'} from ${configured.source}`}
      </p>
      <ul className="mt-2 space-y-1">
        {rules.map((rule) => (
          <li key={rule.rule_id} className="flex flex-wrap items-baseline gap-1.5">
            <span
              className={clsx(
                'rounded px-1 py-0.5 font-mono text-[10px]',
                rule.action === 'allow'
                  ? 'bg-emerald-500/15 text-emerald-300'
                  : 'bg-status-error/15 text-status-error',
              )}
            >
              {rule.action}
            </span>
            <span className="font-mono text-slate-300">{rule.category}</span>
            <span className="text-slate-500">{rule.rule_id}</span>
            {rule.max_value_native && (
              <span className="text-slate-500">max {rule.max_value_native}</span>
            )}
            {rule.max_value_usd != null && (
              <span className="text-slate-500">max ${rule.max_value_usd}</span>
            )}
            {rule.allowed_networks && (
              <span className="text-slate-500">{rule.allowed_networks.join(', ')}</span>
            )}
            {rule.allowed_tokens && (
              <span className="text-slate-500">tokens: {rule.allowed_tokens.join(', ')}</span>
            )}
          </li>
        ))}
      </ul>
      <p className="mt-2 text-slate-500">
        The emergency stop overrides every rule here and cannot be re-enabled by a policy.
      </p>
    </div>
  )
}

function MessageDetail({ message }: { message: ChatMessage }) {
  if (message.role === 'user' || message.error) return null
  const hasDetail =
    message.toolCalls?.length ||
    message.approvals?.length ||
    message.blockers?.length ||
    message.needsUserAction?.length ||
    message.truncated
  if (!hasDetail) return null

  return (
    <>
      <ToolCallList calls={message.toolCalls ?? []} />
      <ApprovalNotice message={message} />
      {message.needsUserAction?.map((reason) => (
        <p
          key={reason}
          className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
        >
          Needs you: {reason}
        </p>
      ))}
      {message.blockers?.map((blocker) => (
        <p
          key={blocker}
          className="mt-2 rounded-lg border border-status-error/30 bg-status-error/10 px-3 py-2 text-xs text-status-error"
        >
          Blocked: {blocker}
        </p>
      ))}
      {message.truncated && (
        <p className="mt-2 rounded-lg border border-surface-300 bg-slate-900/40 px-3 py-2 text-xs text-slate-400">
          Stopped early: a bound was reached. This turn is incomplete.
        </p>
      )}
    </>
  )
}

function toolLabel(tool: LiveTool): string {
  if (tool.status === 'running') return 'running…'
  const took = tool.durationMs !== undefined ? ` · ${(tool.durationMs / 1000).toFixed(1)}s` : ''
  return `${tool.status === 'success' ? 'done' : tool.status.replace(/_/g, ' ')}${took}`
}

/**
 * The agent's turn while it is still being produced: the answer so far and which tools
 * are running. Display only. It is a preview of the stream; the message that replaces it
 * is built from the backend's final result, and a tool is shown as finished only because
 * the backend said so.
 */
function LiveReply({ live }: { live: LiveTurn | null }) {
  const hasText = !!live && live.text.length > 0
  const hasTools = !!live && live.tools.length > 0

  return (
    <div className="flex flex-col gap-2">
      {hasTools && (
        <ul className="space-y-1 rounded-2xl border border-surface-300 bg-surface-200/60 px-3 py-2 text-xs">
          {live!.tools.map((t) => (
            <li key={t.id} className="flex items-center gap-2 text-slate-300">
              <span
                className={clsx(
                  'h-1.5 w-1.5 shrink-0 rounded-full',
                  t.status === 'running'
                    ? 'animate-pulse bg-sky-400'
                    : t.status === 'success'
                      ? 'bg-emerald-400'
                      : 'bg-amber-400',
                )}
              />
              <span className="font-mono">{t.tool}</span>
              <span className="text-slate-500">{toolLabel(t)}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="whitespace-pre-wrap break-words text-[15px] leading-7 text-slate-100">
        {hasText ? (
          <>
            {live!.text}
            <span className="ml-0.5 inline-block h-4 w-1.5 translate-y-0.5 animate-pulse bg-slate-300" aria-hidden />
          </>
        ) : (
          <span className="animate-pulse text-sm text-slate-400">
            {hasTools ? 'Working…' : 'Thinking…'}
          </span>
        )}
      </div>
    </div>
  )
}

/** Small "copy reply" action under an assistant message (ChatGPT-style). */
function CopyReply({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }
  return (
    <button
      type="button"
      onClick={copy}
      aria-label="Copy reply"
      className="rounded-lg p-1.5 text-slate-500 transition hover:bg-surface-300/70 hover:text-slate-200"
    >
      {copied ? <Check className="h-4 w-4 text-emerald-400" /> : <Copy className="h-4 w-4" />}
    </button>
  )
}

export default function AgentChat() {
  // Conversation lives in a persistent external store, not component state, so
  // it survives navigating to another page and back.
  const { messages, pending, live } = useSyncExternalStore(
    agentChatStore.subscribe,
    agentChatStore.getSnapshot,
  )
  const location = useLocation()
  const navigate = useNavigate()
  // Other screens can hand over a draft (e.g. "define a goal"); it is never auto-sent.
  const [input, setInput] = useState(() => (location.state as { prompt?: string } | null)?.prompt ?? '')
  const [showInfo, setShowInfo] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const { data: info } = useQuery({ queryKey: ['agent-info'], queryFn: api.agentInfo })
  // Display only: every allow/deny is decided by the backend policy.
  const { data: autonomy } = useQuery({
    queryKey: ['agent-autonomy'],
    queryFn: api.autonomy,
  })

  // While text is streaming in, a smooth scroll restarts on every token and lags behind it.
  const liveSize = live ? live.text.length + live.tools.length : 0
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: liveSize > 0 ? 'auto' : 'smooth' })
  }, [messages.length, pending, liveSize])

  // ChatGPT-style composer: the box grows with the text up to a cap, then scrolls.
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    const max = 200
    el.style.height = `${Math.min(el.scrollHeight, max)}px`
    el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden'
  }, [input])

  const send = useMutation({
    mutationFn: ({
      message,
      history,
    }: {
      message: string
      history: Array<{ role: 'user' | 'assistant'; content: string }>
    }) => {
      // The turn streams: reply text and tool progress arrive live and are shown as a
      // preview. What is kept is the final result this resolves with, i.e. exactly what the
      // blocking endpoint would have returned.
      const epoch = agentChatStore.getEpoch()
      const controller = new AbortController()
      agentChatStore.setAbort(() => controller.abort())
      return api
        .agentChatStream(
          message,
          history,
          (event) => agentChatStore.applyStreamEvent(event, epoch),
          controller.signal,
        )
        .finally(() => agentChatStore.setAbort(null))
    },
    onMutate: () => {
      agentChatStore.setPending(true)
      // Remember which conversation this request belongs to.
      return { epoch: agentChatStore.getEpoch() }
    },
    onSuccess: (response, _vars, context) => {
      // The chat was cleared while the agent was still working: drop the late reply.
      if (context && context.epoch !== agentChatStore.getEpoch()) return
      // Everything rendered below comes from this response, which came from real
      // tool invocations. The UI does not infer success from the prose.
      agentChatStore.add({
        role: 'agent',
        content: response.reply,
        taskId: response.task?.task_id,
        taskStatus: response.task?.status,
        toolCalls: response.tool_calls,
        approvals: response.approvals_required,
        blockers: response.blockers,
        needsUserAction: response.needs_user_action,
        truncated: response.truncated,
      })
    },
    onError: (error: Error, _vars, context) => {
      if (context && context.epoch !== agentChatStore.getEpoch()) return
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
    if (!window.confirm('Clear this conversation?')) return
    agentChatStore.clear()
    // Also drop the draft in the box and any prompt handed over by another page (it lives in the
    // router's history state and would otherwise come back on reload).
    setInput('')
    if (location.state) navigate(location.pathname, { replace: true, state: null })
  }

  const fillPrompt = (prompt: string) => {
    setInput(prompt)
    inputRef.current?.focus()
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <header className="flex items-center justify-between gap-3 px-4 pb-2 pt-1 lg:px-0">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-slate-100 lg:text-2xl">AI Agent Console</h1>
          <p className="mt-0.5 flex items-center gap-2 text-xs text-slate-400">
            <span className={clsx('h-1.5 w-1.5 rounded-full', info ? 'bg-emerald-400' : 'bg-slate-500')} />
            {info ? 'Online' : 'Connecting'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowInfo((v) => !v)}
            aria-pressed={showInfo}
            aria-label="Model and autonomy information"
            className={clsx(
              'flex h-10 w-10 items-center justify-center rounded-xl border border-surface-300 text-slate-300 hover:bg-surface-200',
              showInfo && 'border-accent text-accent',
            )}
          >
            <Info className="h-5 w-5" />
          </button>
          {messages.length > 0 && (
            <button type="button" className="btn-ghost" onClick={clearChat}>
              <Trash2 className="h-4 w-4" />
              <span className="hidden sm:inline">Clear chat</span>
            </button>
          )}
        </div>
      </header>

      {/* Model + autonomy details, tucked away so the conversation stays clean */}
      {showInfo && (
        <div className="mx-auto w-full max-w-3xl space-y-2 px-4 pb-2 lg:px-0">
          {info && (
            <Alert kind="info" title="Single fixed model">
              All reasoning runs on <span className="font-mono">{info.model_id}</span> at{' '}
              <span className="font-mono">{info.base_url}</span>. There is no model switching,
              fallback, or routing. API key is configured but never returned to the browser.
            </Alert>
          )}
          {autonomy && <AutonomyPanel policy={autonomy} />}
        </div>
      )}

      {/* Conversation: one centred column, like ChatGPT */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col gap-6 px-4 py-4 lg:px-0">
          {messages.length === 0 && !pending && (
            <div className="flex flex-1 flex-col items-center justify-center gap-6 py-10 text-center">
              <h2 className="text-2xl font-semibold text-slate-100 sm:text-3xl">What can I help with?</h2>
              <div className="flex max-w-xl flex-wrap justify-center gap-2">
                {QUICK_ACTIONS.map(({ label, icon: Icon, prompt }) => (
                  <button
                    key={label}
                    type="button"
                    onClick={() => fillPrompt(prompt)}
                    className="flex items-center gap-2 rounded-full border border-surface-300 bg-surface-200/60 px-4 py-2 text-sm text-slate-200 transition hover:bg-surface-200"
                  >
                    <Icon className="h-4 w-4 text-slate-400" />
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m) => {
            const mine = m.role === 'user'
            if (mine) {
              return (
                <div key={m.id} className="flex justify-end">
                  <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-3xl bg-surface-300 px-5 py-2.5 text-[15px] leading-7 text-slate-100 sm:max-w-[75%]">
                    {m.content}
                  </div>
                </div>
              )
            }
            return (
              <div key={m.id} className="group flex flex-col items-start">
                <div
                  className={clsx(
                    'w-full whitespace-pre-wrap break-words text-[15px] leading-7 text-slate-100',
                    m.error && 'rounded-2xl border border-status-error/40 bg-status-error/10 px-4 py-3',
                  )}
                >
                  {m.content}
                  {m.taskId && (
                    <div className="mt-2 inline-flex flex-wrap items-center gap-1.5 rounded-md bg-emerald-500/15 px-2 py-1 font-mono text-xs text-emerald-300">
                      {m.taskId}
                      {m.taskStatus && <span className="font-sans text-[11px] opacity-80">{m.taskStatus}</span>}
                    </div>
                  )}
                  <MessageDetail message={m} />
                </div>
                <div className="mt-1 flex items-center gap-1 text-[11px] text-slate-500">
                  {!m.error && <CopyReply text={m.content} />}
                  <span className="px-1">{clock(m.at)}</span>
                </div>
              </div>
            )
          })}

          {pending && <LiveReply live={live} />}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Composer: rounded pill, centred */}
      <div className="px-4 pb-2 pt-1 lg:px-0">
        <form
          onSubmit={handleSubmit}
          className="mx-auto flex w-full max-w-3xl items-end gap-2 rounded-[28px] border border-surface-300 bg-surface-200 py-2 pl-5 pr-2 shadow-lg shadow-black/20 focus-within:border-slate-500"
        >
          <textarea
            ref={inputRef}
            rows={1}
            className="max-h-[200px] min-h-[2.5rem] min-w-0 flex-1 resize-none self-center border-0 bg-transparent py-2 text-base text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-0 lg:text-[15px]"
            placeholder="Ask anything"
            aria-label="Message to the agent"
            enterKeyHint="send"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends; Shift+Enter is a newline, so a multi-line address or
              // amount can still be pasted in one piece.
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                handleSubmit(e as unknown as React.FormEvent)
              }
            }}
            disabled={pending}
          />
          <button
            type="submit"
            aria-label="Send"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
            disabled={!input.trim() || pending}
          >
            <ArrowUp className="h-5 w-5" />
          </button>
        </form>
        <p className="mx-auto mt-2 max-w-3xl text-center text-[11px] text-slate-500">
          The agent can make mistakes. Check important info.
        </p>
      </div>
    </div>
  )
}
