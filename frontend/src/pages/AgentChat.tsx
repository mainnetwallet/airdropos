/** AI Agent Console: natural-language chat with the central agent (dark "Agent Chat" screen on mobile). */

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  Bot,
  Check,
  Copy,
  Info,
  ListChecks,
  Plus,
  Search,
  Send,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  Wallet,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '@/services/api'
import { agentChatStore, type ChatMessage, type LiveTool, type LiveTurn } from '@/services/agentChatStore'
import { Alert, Card } from '@/components/ui'
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

function AgentAvatar() {
  return (
    <span className="mt-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-b from-sky-400 to-accent-hover text-white ring-2 ring-sky-300/30 lg:hidden">
      <Bot className="h-5 w-5" />
    </span>
  )
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
    <div className="flex items-start gap-2">
      <AgentAvatar />
      <div className="flex max-w-[82%] flex-col gap-2 lg:max-w-[80%]">
        {hasTools && (
          <ul className="space-y-1 rounded-2xl border border-surface-300 bg-surface-200/60 px-3 py-2 text-xs lg:border-0">
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
        <div className="whitespace-pre-wrap break-words rounded-2xl rounded-tl-md border border-surface-300 bg-surface-200 px-4 py-2.5 text-[15px] leading-relaxed text-slate-100 lg:border-0 lg:text-sm lg:text-slate-200">
          {hasText ? (
            <>
              {live!.text}
              <span className="ml-0.5 inline-block h-4 w-1.5 translate-y-0.5 animate-pulse bg-sky-400/80" aria-hidden />
            </>
          ) : (
            <span className="animate-pulse text-sm text-slate-400">
              {hasTools ? 'Working…' : 'Thinking…'}
            </span>
          )}
        </div>
      </div>
    </div>
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
  // Telegram account the task created by the next message must use ('' = the default account).
  const [telegramAccount, setTelegramAccount] = useState('')
  const telegramAccounts = useQuery({ queryKey: ['telegram-accounts'], queryFn: api.telegramAccounts })
  const loggedInAccounts = (telegramAccounts.data?.accounts ?? []).filter((a) => a.logged_in)
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
      account,
    }: {
      message: string
      history: Array<{ role: 'user' | 'assistant'; content: string }>
      account?: string
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
          account,
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
    send.mutate({ message: text, history, account: telegramAccount || undefined })
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

      {autonomy && (
        <div className={clsx(!showInfo && 'hidden lg:block')}>
          <AutonomyPanel policy={autonomy} />
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
                  wallets”, “Show me the status of AIR-2026-10-15-483920”, or “Report today's activity”.
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
                      <div className="mt-2 inline-flex flex-wrap items-center gap-1.5 rounded-md bg-emerald-500/15 px-2 py-1 font-mono text-xs text-emerald-300">
                        {m.taskId}
                        {m.taskStatus && <span className="font-sans text-[11px] opacity-80">{m.taskStatus}</span>}
                      </div>
                    )}
                    <MessageDetail message={m} />
                  </div>
                  <span className="mt-1 px-1 text-[11px] text-slate-500 lg:hidden">{clock(m.at)}</span>
                </div>
              </div>
            )
          })}

          {pending && <LiveReply live={live} />}
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

        {loggedInAccounts.length > 0 && (
          <label className="mb-2 flex items-center gap-2 text-xs text-slate-400">
            <span className="shrink-0">Telegram for new tasks</span>
            <select
              value={telegramAccount}
              onChange={(e) => setTelegramAccount(e.target.value)}
              disabled={pending}
              aria-label="Telegram account for tasks created from this chat"
              className="min-w-0 flex-1 rounded-lg border border-surface-300 bg-surface-200 px-2 py-1.5 text-xs text-slate-100 focus:border-accent focus:outline-none"
            >
              <option value="">Default account</option>
              {loggedInAccounts.map((a) => (
                <option key={a.label} value={a.label}>
                  {a.label}
                  {a.username ? ` (@${a.username})` : ''}
                </option>
              ))}
            </select>
          </label>
        )}

        <form
          onSubmit={handleSubmit}
          className="flex items-end gap-2 lg:border-t lg:border-surface-300 lg:pt-3"
        >
          <textarea
            ref={inputRef}
            rows={1}
            className="max-h-[200px] min-h-[2.75rem] min-w-0 flex-1 resize-none rounded-3xl border border-surface-300 bg-surface-200 px-4 py-3 text-base text-slate-100 placeholder-slate-500 focus:border-accent focus:outline-none lg:rounded-lg lg:py-2 lg:text-sm"
            placeholder="Type a message…"
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
