/** AI Agent Console: natural-language chat with the central agent (dark "Agent Chat" screen on mobile). */

import { memo, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useLocation, useNavigate, Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowUp,
  ChevronRight,
  MessageSquare,
  PanelLeft,
  Pencil,
  FileText,
  Plus,
  RefreshCw,
  Square,
  X,
  Check,
  Copy,
  Info,
  ListChecks,
  Search,
  ShieldAlert,
  Trash2,
  Wallet,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '@/services/api'
import {
  agentChatStore,
  type ChatMessage,
  type ConversationSummary,
  type LiveTool,
  type LiveTurn,
} from '@/services/agentChatStore'
import {
  MAX_FILES,
  MAX_IMAGES,
  composeMessage,
  readAttachment,
  type PendingAttachment,
} from '@/services/chatAttachments'
import ConfirmModal from '@/components/ConfirmModal'
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
 * What the status at task-creation time actually means, so "READY" is not read as
 * "finished". A task created here is queued: the worker claims it, runs it, and may
 * return it to the queue between execution windows.
 */
const TASK_STATUS_HINTS: Record<string, string> = {
  CREATED: 'created',
  READY: 'queued — the worker will pick it up',
  RESEARCHING: 'researching',
  PLANNING: 'planning',
  IN_PROGRESS: 'running in the background',
  WAITING: 'waiting on a condition',
  RETRYING: 'will be retried automatically',
  NEEDS_USER_ACTION: 'needs you — see Agent Inbox',
  PARTIALLY_COMPLETED: 'partly done',
  COMPLETED: 'done',
  FAILED: 'stopped',
  CANCELLED: 'cancelled',
  UNVERIFIED: 'ran, but not verified',
}

/** One tool call, with its real outcome. */
function ToolCallRow({ call }: { call: AgentToolCall }) {
  return (
    <div className="rounded-lg border border-surface-300 bg-slate-900/40 px-2.5 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-xs text-slate-300">{call.tool}</span>
        <span className={clsx('shrink-0 font-mono text-[11px]', TOOL_STATUS_STYLE[call.status])}>
          {call.status}
        </span>
      </div>
      {call.error && <p className="mt-1 break-words text-xs text-slate-400">{call.error}</p>}
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
  )
}

/**
 * The tools the agent actually ran, with their real outcomes.
 *
 * The list is folded to one summary line so a long run does not bury the answer. Two things are
 * never folded away: if any call did not succeed the list opens by itself (a failure, a refusal
 * and an "unavailable" are shown as themselves, never as a tick), and the addresses and
 * transaction hashes the backend returned stay visible and copyable.
 */
function ToolCallList({ calls }: { calls: AgentToolCall[] }) {
  const problems = calls.filter((c) => c.status !== 'success').length
  const [open, setOpen] = useState(problems > 0)
  if (calls.length === 0) return null

  const highlights: string[] = []
  for (const call of calls) {
    if (call.status !== 'success') continue
    if (typeof call.data?.tx_hash === 'string') highlights.push(call.data.tx_hash)
    const address = addressOf(call.data)
    if (address) highlights.push(address)
  }

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded-lg border border-surface-300 bg-slate-900/40 px-2.5 py-1.5 text-left text-xs text-slate-300 transition hover:bg-slate-900/70"
      >
        <ChevronRight className={clsx('h-3.5 w-3.5 shrink-0 transition-transform', open && 'rotate-90')} />
        <span>
          {calls.length} tool{calls.length === 1 ? '' : 's'} used
        </span>
        <span className={clsx('ml-auto font-mono text-[11px]', problems ? 'text-amber-300' : 'text-emerald-300')}>
          {problems ? `${problems} did not succeed` : 'all succeeded'}
        </span>
      </button>
      {open ? (
        <div className="mt-1.5 space-y-1.5">
          {calls.map((call, i) => (
            <ToolCallRow key={`${call.tool}-${i}`} call={call} />
          ))}
        </div>
      ) : (
        highlights.map((value, i) => <CopyableAddress key={`${value}-${i}`} address={value} />)
      )}
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
      <div className="break-words text-[15px] leading-7 text-slate-100">
        {hasText ? (
          <>
            <Markdown text={live!.text} />
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

const REMARK_PLUGINS = [remarkGfm]
const MD_COMPONENTS = {
  a: ({ node: _node, ...props }: any) => <a {...props} target="_blank" rel="noopener noreferrer" />,
}

/** Agent text as Markdown (lists, code, tables, links). Raw HTML is never rendered. */
const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div
      className={clsx(
        'break-words [&>*:first-child]:mt-0 [&>*:last-child]:mb-0',
        '[&_p]:my-2 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-6 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-6 [&_li]:my-0.5',
        '[&_h1]:mb-2 [&_h1]:mt-4 [&_h1]:text-xl [&_h1]:font-semibold [&_h2]:mb-2 [&_h2]:mt-4 [&_h2]:text-lg [&_h2]:font-semibold [&_h3]:mb-1 [&_h3]:mt-3 [&_h3]:font-semibold',
        '[&_a]:text-sky-400 [&_a]:underline [&_blockquote]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-surface-300 [&_blockquote]:pl-3 [&_blockquote]:text-slate-300',
        '[&_pre]:my-3 [&_pre]:overflow-x-auto [&_pre]:rounded-xl [&_pre]:bg-slate-950/70 [&_pre]:p-3 [&_pre]:text-[13px]',
        '[&_:not(pre)>code]:rounded [&_:not(pre)>code]:bg-surface-300/70 [&_:not(pre)>code]:px-1 [&_:not(pre)>code]:py-0.5 [&_:not(pre)>code]:font-mono [&_:not(pre)>code]:text-[13px]',
        '[&_table]:my-3 [&_table]:block [&_table]:overflow-x-auto [&_td]:border [&_td]:border-surface-300 [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-surface-300 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left',
      )}
    >
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        components={MD_COMPONENTS}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
})

/** Thumbnail (photo) or name chip (file) for an attachment. `onRemove` only in the composer. */
function AttachmentChip({
  name,
  kind,
  thumb,
  onRemove,
}: {
  name: string
  kind: 'image' | 'file'
  thumb?: string
  onRemove?: () => void
}) {
  return (
    <div className="relative flex items-center gap-2 rounded-xl border border-surface-300 bg-surface-100/60 p-1.5 pr-3 text-xs text-slate-200">
      {kind === 'image' && thumb ? (
        <img src={thumb} alt={name} className="h-12 w-12 rounded-lg object-cover" />
      ) : (
        <span className="flex h-12 w-12 items-center justify-center rounded-lg bg-surface-300/70 text-slate-300">
          <FileText className="h-5 w-5" />
        </span>
      )}
      <span className="max-w-[10rem] truncate">{name}</span>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${name}`}
          className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-slate-200 text-slate-900 hover:bg-white"
        >
          <X className="h-3 w-3" />
        </button>
      )}
    </div>
  )
}

function dayLabel(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(date).setHours(0, 0, 0, 0)) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** Saved conversations: open one, start a new one, or delete one. */
function HistorySidebar({
  open,
  conversations,
  activeId,
  onNew,
  onOpen,
  onDelete,
  onClose,
}: {
  open: boolean
  conversations: ConversationSummary[]
  activeId: string
  onNew: () => void
  onOpen: (id: string) => void
  onDelete: (id: string) => void
  onClose: () => void
}) {
  if (!open) return null
  return (
    <>
      <div className="fixed inset-0 z-30 bg-black/60 lg:hidden" onClick={onClose} aria-hidden />
      <aside
        aria-label="Conversation history"
        className="fixed inset-y-0 left-0 z-40 flex w-72 flex-col gap-2 border-r border-surface-300 bg-surface-100 p-3 lg:static lg:z-auto lg:mr-4 lg:w-64 lg:shrink-0 lg:rounded-xl lg:border"
      >
        <button
          type="button"
          onClick={onNew}
          className="flex items-center gap-2 rounded-xl border border-surface-300 px-3 py-2 text-sm font-medium text-slate-100 transition hover:bg-surface-200"
        >
          <Plus className="h-4 w-4" />
          New chat
        </button>
        <p className="px-1 pt-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">History</p>
        <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto">
          {conversations.length === 0 && (
            <li className="px-2 py-3 text-xs text-slate-500">Your conversations will be saved here.</li>
          )}
          {conversations.map((c) => (
            <li key={c.id} className="group relative">
              <button
                type="button"
                onClick={() => onOpen(c.id)}
                aria-current={c.id === activeId ? 'true' : undefined}
                className={clsx(
                  'flex w-full items-start gap-2 rounded-lg px-2 py-2 pr-9 text-left transition',
                  c.id === activeId ? 'bg-surface-300 text-slate-100' : 'text-slate-300 hover:bg-surface-200',
                )}
              >
                <MessageSquare className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                <span className="min-w-0">
                  <span className="block truncate text-sm">{c.title}</span>
                  <span className="block text-[11px] text-slate-500">{dayLabel(c.updatedAt)}</span>
                </span>
              </button>
              <button
                type="button"
                onClick={() => onDelete(c.id)}
                aria-label={`Delete conversation ${c.title}`}
                className="absolute right-1.5 top-2 rounded-md p-1 text-slate-500 opacity-100 transition hover:bg-surface-100 hover:text-status-error lg:opacity-0 lg:group-hover:opacity-100 lg:focus:opacity-100"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </li>
          ))}
        </ul>
      </aside>
    </>
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
  const { messages, pending, busy, live, activeId, conversations } = useSyncExternalStore(
    agentChatStore.subscribe,
    agentChatStore.getSnapshot,
  )
  const location = useLocation()
  const navigate = useNavigate()
  // Other screens can hand over a draft (e.g. "define a goal"); it is never auto-sent.
  const [input, setInput] = useState(() => (location.state as { prompt?: string } | null)?.prompt ?? '')
  const [showInfo, setShowInfo] = useState(false)
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])
  const [attachError, setAttachError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  // The history sidebar starts closed on every screen size; the header button opens it.
  const [historyOpen, setHistoryOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [toDelete, setToDelete] = useState<ConversationSummary | null>(null)
  const [editText, setEditText] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)
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
  }, [messages.length, pending, liveSize, activeId])

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
      images,
      rewind,
    }: {
      message: string
      history: Array<{ role: 'user' | 'assistant'; content: string }>
      images?: string[]
      /** Edit/regenerate: the server keeps only this many stored messages first. */
      rewind?: boolean
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
          undefined,
          images,
          // The server keeps this conversation's history itself, keyed by this id.
          agentChatStore.getSnapshot().activeId,
          rewind ? history.length : undefined,
        )
        .finally(() => agentChatStore.setAbort(null))
    },
    onMutate: () => {
      // Remember which conversation this request belongs to: the reply lands there even if the
      // operator opens another conversation while the agent works.
      const conversationId = agentChatStore.getSnapshot().activeId
      agentChatStore.setPending(true, conversationId)
      return { epoch: agentChatStore.getEpoch(), conversationId }
    },
    onSuccess: (response, _vars, context) => {
      // The chat was cleared while the agent was still working: drop the late reply.
      if (context && context.epoch !== agentChatStore.getEpoch()) return
      // Everything rendered below comes from this response, which came from real
      // tool invocations. The UI does not infer success from the prose.
      agentChatStore.add(
        {
          role: 'agent',
          content: response.reply,
          taskId: response.task?.task_id,
          taskStatus: response.task?.status,
          toolCalls: response.tool_calls,
          approvals: response.approvals_required,
          blockers: response.blockers,
          needsUserAction: response.needs_user_action,
          truncated: response.truncated,
        },
        context?.conversationId,
      )
    },
    onError: (error: Error, _vars, context) => {
      if (context && context.epoch !== agentChatStore.getEpoch()) return
      if (error.name === 'AbortError') {
        agentChatStore.add({ role: 'agent', content: 'Stopped.', error: true }, context?.conversationId)
        return
      }
      agentChatStore.add(
        {
          role: 'agent',
          content: `Request failed: ${error.message}`,
          error: true,
        },
        context?.conversationId,
      )
    },
    onSettled: () => agentChatStore.setPending(false),
  })

  const historyBefore = (dropLastUser = false) => {
    const kept = agentChatStore.getSnapshot().messages.filter((m) => !m.error)
    const list = dropLastUser ? kept.slice(0, kept.map((m) => m.role).lastIndexOf('user')) : kept
    return list.map((m) => ({
      role: m.role === 'user' ? ('user' as const) : ('assistant' as const),
      content: m.content,
    }))
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    const { content, display, images } = composeMessage(input, attachments)
    if (!content.trim()) return

    // Everything said so far (before this message) is sent along so the agent
    // remembers the conversation. Failed requests are not part of it.
    const history = historyBefore()

    agentChatStore.add({
      role: 'user',
      content,
      display: attachments.length ? display : undefined,
      attachments: attachments.length
        ? attachments.map((a) => ({ name: a.name, kind: a.kind, thumb: a.dataUrl }))
        : undefined,
    })
    setInput('')
    setAttachments([])
    setAttachError(null)
    send.mutate({ message: content, history, images })
  }

  /** Replace the last reply with a fresh one for the same question. */
  const regenerate = () => {
    if (busy) return
    const all = agentChatStore.getSnapshot().messages
    const last = all[all.length - 1]
    const lastUser = [...all].reverse().find((m) => m.role === 'user')
    if (!last || last.role !== 'agent' || !lastUser) return
    const history = historyBefore(true)
    agentChatStore.removeLast()
    send.mutate({ message: lastUser.content, history, rewind: true })
  }

  const addFiles = async (files: FileList | File[]) => {
    setAttachError(null)
    const next = [...attachments]
    const problems: string[] = []
    for (const file of Array.from(files)) {
      const result = await readAttachment(file)
      if (result.error || !result.attachment) {
        problems.push(result.error ?? 'Could not read that file.')
        continue
      }
      const isImg = result.attachment.kind === 'image'
      const count = next.filter((a) => (a.kind === 'image') === isImg).length
      if (count >= (isImg ? MAX_IMAGES : MAX_FILES)) {
        problems.push(isImg ? `At most ${MAX_IMAGES} photos per message.` : `At most ${MAX_FILES} files per message.`)
        continue
      }
      next.push(result.attachment)
    }
    setAttachments(next)
    if (problems.length) setAttachError(problems.join(' '))
  }

  /** Replace an earlier message with an edited one: it and everything after it is dropped. */
  const resendEdited = (messageId: string) => {
    const text = editText.trim()
    if (!text || busy) return
    const all = agentChatStore.getSnapshot().messages
    const index = all.findIndex((m) => m.id === messageId)
    if (index < 0) return
    const history = all
      .slice(0, index)
      .filter((m) => !m.error)
      .map((m) => ({
        role: m.role === 'user' ? ('user' as const) : ('assistant' as const),
        content: m.content,
      }))
    agentChatStore.truncateFrom(messageId)
    agentChatStore.add({ role: 'user', content: text })
    setEditingId(null)
    send.mutate({ message: text, history, rewind: true })
  }

  const resetComposer = () => {
    setInput('')
    setAttachments([])
    setAttachError(null)
    setEditingId(null)
    // A prompt handed over by another page lives in the router's history state and would
    // otherwise come back on reload.
    if (location.state) navigate(location.pathname, { replace: true, state: null })
  }
  const closeHistoryOnMobile = () => {
    if (!window.matchMedia('(min-width: 1024px)').matches) setHistoryOpen(false)
  }
  const openConversation = (id: string) => {
    agentChatStore.switchTo(id)
    resetComposer()
    closeHistoryOnMobile()
  }
  const newConversation = () => {
    agentChatStore.newConversation()
    resetComposer()
    closeHistoryOnMobile()
  }
  const confirmDelete = () => {
    if (!toDelete) return
    agentChatStore.deleteConversation(toDelete.id)
    // Also remove the server's copy (its messages only; tasks are never touched). A conversation
    // that was never sent anywhere has none, so a 404 here is fine.
    void api.deleteAgentConversation(toDelete.id).catch(() => undefined)
    setToDelete(null)
    resetComposer()
  }

  const fillPrompt = (prompt: string) => {
    setInput(prompt)
    inputRef.current?.focus()
  }

  return (
    <div
      className="relative flex h-full min-h-0"
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes('Files')) {
          e.preventDefault()
          setDragging(true)
        }
      }}
      onDragLeave={(e) => {
        if (e.currentTarget === e.target) setDragging(false)
      }}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return
        e.preventDefault()
        setDragging(false)
        void addFiles(e.dataTransfer.files)
      }}
    >
      {dragging && (
        <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center rounded-2xl border-2 border-dashed border-accent bg-slate-950/70 text-lg font-medium text-slate-100">
          Drop photos or text files to attach
        </div>
      )}
      {toDelete && (
        <ConfirmModal
          title="Delete conversation"
          confirmLabel="Delete"
          onConfirm={confirmDelete}
          onClose={() => setToDelete(null)}
        >
          <p>
            Delete <span className="font-semibold text-slate-100">“{toDelete.title}”</span>?
          </p>
          <p className="mt-1 text-slate-400">
            Its {toDelete.messageCount} message{toDelete.messageCount === 1 ? '' : 's'} will be removed from
            this browser. This cannot be undone.
            {busy && ' If the agent is working in it, that turn is stopped.'}
          </p>
        </ConfirmModal>
      )}
      <HistorySidebar
        open={historyOpen}
        conversations={conversations}
        activeId={activeId}
        onNew={newConversation}
        onOpen={openConversation}
        onDelete={(id) => setToDelete(conversations.find((c) => c.id === id) ?? null)}
        onClose={() => setHistoryOpen(false)}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {/* Header */}
      <header className="flex items-center justify-between gap-3 px-4 pb-2 pt-1 lg:px-0">
        <div className="flex min-w-0 items-center gap-3">
          <button
            type="button"
            onClick={() => setHistoryOpen((v) => !v)}
            aria-pressed={historyOpen}
            aria-label="Conversation history"
            title="Conversation history"
            className={clsx(
              'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-surface-300 text-slate-300 hover:bg-surface-200',
              historyOpen && 'border-accent text-accent',
            )}
          >
            <PanelLeft className="h-5 w-5" />
          </button>
          <div className="min-w-0">
            <h1 className="text-xl font-semibold text-slate-100 lg:text-2xl">AI Agent Console</h1>
            <p className="mt-0.5 flex items-center gap-2 text-xs text-slate-400">
              <span className={clsx('h-1.5 w-1.5 rounded-full', info ? 'bg-emerald-400' : 'bg-slate-500')} />
              {info ? 'Online' : 'Connecting'}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={newConversation}
            aria-label="New chat"
            title="New chat"
            className="flex h-10 items-center justify-center gap-2 rounded-xl border border-surface-300 px-3 text-sm font-medium text-slate-100 transition hover:bg-surface-200"
          >
            <Plus className="h-5 w-5" />
            <span className="hidden sm:inline">New chat</span>
          </button>
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
              const canEdit = !busy && !m.attachments?.length
              if (editingId === m.id) {
                return (
                  <div key={m.id} className="flex justify-end">
                    <div className="w-full max-w-[85%] rounded-3xl border border-surface-300 bg-surface-200 p-3 sm:max-w-[75%]">
                      <textarea
                        autoFocus
                        rows={3}
                        value={editText}
                        onChange={(e) => setEditText(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && !e.shiftKey) {
                            e.preventDefault()
                            resendEdited(m.id)
                          }
                          if (e.key === 'Escape') setEditingId(null)
                        }}
                        aria-label="Edit your message"
                        className="w-full resize-y border-0 bg-transparent px-1 text-[15px] leading-7 text-slate-100 focus:outline-none focus:ring-0"
                      />
                      <p className="px-1 text-[11px] text-slate-500">
                        Replies after this message will be replaced by a new one.
                      </p>
                      <div className="mt-2 flex justify-end gap-2">
                        <button type="button" className="btn-ghost" onClick={() => setEditingId(null)}>
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="btn-primary"
                          disabled={!editText.trim() || busy}
                          onClick={() => resendEdited(m.id)}
                        >
                          Send
                        </button>
                      </div>
                    </div>
                  </div>
                )
              }
              return (
                <div key={m.id} className="group flex flex-col items-end gap-1.5">
                  {m.attachments && m.attachments.length > 0 && (
                    <div className="flex max-w-[85%] flex-wrap justify-end gap-2 sm:max-w-[75%]">
                      {m.attachments.map((a, i) => (
                        <AttachmentChip key={`${a.name}-${i}`} name={a.name} kind={a.kind} thumb={a.thumb} />
                      ))}
                    </div>
                  )}
                  {(m.attachments?.length ? m.display : m.content) && (
                    <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-3xl bg-surface-300 px-5 py-2.5 text-[15px] leading-7 text-slate-100 sm:max-w-[75%]">
                      {m.attachments?.length ? m.display : m.content}
                    </div>
                  )}
                  {canEdit && (
                    <button
                      type="button"
                      onClick={() => {
                        setEditingId(m.id)
                        setEditText(m.content)
                      }}
                      aria-label="Edit message"
                      title="Edit and resend"
                      className="rounded-lg p-1.5 text-slate-500 transition hover:bg-surface-300/70 hover:text-slate-200 lg:opacity-0 lg:group-hover:opacity-100 lg:focus:opacity-100"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                  )}
                </div>
              )
            }
            return (
              <div key={m.id} className="group flex flex-col items-start">
                <div
                  className={clsx(
                    'w-full break-words text-[15px] leading-7 text-slate-100',
                    m.error && 'whitespace-pre-wrap rounded-2xl border border-status-error/40 bg-status-error/10 px-4 py-3',
                  )}
                >
                  {m.error ? m.content : <Markdown text={m.content} />}
                  {m.taskId && (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <span className="inline-flex flex-wrap items-center gap-1.5 rounded-md bg-emerald-500/15 px-2 py-1 font-mono text-xs text-emerald-300">
                        {m.taskId}
                        {m.taskStatus && (
                          <span className="font-sans text-[11px] opacity-80">
                            {TASK_STATUS_HINTS[m.taskStatus] ?? m.taskStatus}
                          </span>
                        )}
                      </span>
                      {/* The chip used to be inert text: the task ran in the background
                          with its progress, blockers and final outcome somewhere the
                          operator could not reach from here. */}
                      <Link
                        to={`/tasks?task=${encodeURIComponent(m.taskId)}`}
                        className="text-[11px] text-slate-400 underline decoration-dotted underline-offset-2 hover:text-slate-200"
                      >
                        Open in Task Manager
                      </Link>
                      <Link
                        to="/inbox"
                        className="text-[11px] text-slate-400 underline decoration-dotted underline-offset-2 hover:text-slate-200"
                      >
                        Agent Inbox
                      </Link>
                    </div>
                  )}
                  <MessageDetail message={m} />
                </div>
                <div className="mt-1 flex items-center gap-1 text-[11px] text-slate-500">
                  {!m.error && <CopyReply text={m.content} />}
                  {!busy && m.id === messages[messages.length - 1]?.id && !m.error && (
                    <button
                      type="button"
                      onClick={regenerate}
                      aria-label="Regenerate reply"
                      className="rounded-lg p-1.5 text-slate-500 transition hover:bg-surface-300/70 hover:text-slate-200"
                    >
                      <RefreshCw className="h-4 w-4" />
                    </button>
                  )}
                  <span className="px-1">{clock(m.at)}</span>
                </div>
              </div>
            )
          })}

          {pending && <LiveReply live={live} />}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Composer: rounded box, centred */}
      <div className="px-4 pb-2 pt-1 lg:px-0">
        <form
          onSubmit={handleSubmit}
          className="mx-auto w-full max-w-3xl rounded-[28px] border border-surface-300 bg-surface-200 py-2 pl-2 pr-2 shadow-lg shadow-black/20 focus-within:border-slate-500"
        >
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-2 px-3 pb-2 pt-2">
              {attachments.map((a) => (
                <AttachmentChip
                  key={a.id}
                  name={a.name}
                  kind={a.kind}
                  thumb={a.dataUrl}
                  onRemove={() => setAttachments((list) => list.filter((x) => x.id !== a.id))}
                />
              ))}
            </div>
          )}
          <div className="flex items-end gap-2">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              accept="image/*,.txt,.md,.csv,.tsv,.json,.jsonl,.yaml,.yml,.toml,.ini,.env,.log,.xml,.html,.css,.js,.jsx,.ts,.tsx,.py,.sol,.rs,.go,.java,.c,.cpp,.h,.sh,.sql,text/*"
              onChange={(e) => {
                if (e.target.files) void addFiles(e.target.files)
                e.target.value = ''
              }}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={busy}
              aria-label="Attach photos or files"
              title="Attach photos or text files"
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-slate-300 transition hover:bg-surface-300/70 disabled:opacity-40"
            >
              <Plus className="h-5 w-5" />
            </button>
            <textarea
              ref={inputRef}
              rows={1}
              className="max-h-[200px] min-h-[2.5rem] min-w-0 flex-1 resize-none self-center border-0 bg-transparent py-2 text-base text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-0 lg:text-[15px]"
              placeholder={busy && !pending ? 'Another conversation is still working…' : 'Ask anything'}
              aria-label="Message to the agent"
              enterKeyHint="send"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onPaste={(e) => {
                const files = Array.from(e.clipboardData.files)
                if (files.length) {
                  e.preventDefault()
                  void addFiles(files)
                }
              }}
              onKeyDown={(e) => {
                // Enter sends; Shift+Enter is a newline, so a multi-line address or
                // amount can still be pasted in one piece.
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  handleSubmit(e as unknown as React.FormEvent)
                }
              }}
              disabled={busy}
            />
            {busy ? (
              <button
                type="button"
                onClick={() => agentChatStore.stop()}
                aria-label="Stop"
                title="Stop"
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-slate-200 text-slate-900 transition hover:bg-white"
              >
                <Square className="h-4 w-4 fill-current" />
              </button>
            ) : (
              <button
                type="submit"
                aria-label="Send"
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
                disabled={!input.trim() && attachments.length === 0}
              >
                <ArrowUp className="h-5 w-5" />
              </button>
            )}
          </div>
        </form>
        {attachError && (
          <p role="alert" className="mx-auto mt-2 max-w-3xl text-center text-xs text-amber-300">
            {attachError}
          </p>
        )}
        <p className="mx-auto mt-2 max-w-3xl text-center text-[11px] text-slate-500">
          The agent can make mistakes. Check important info.
        </p>
      </div>
      </div>
    </div>
  )
}
