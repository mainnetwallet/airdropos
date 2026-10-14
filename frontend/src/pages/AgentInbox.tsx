/**
 * Agent Inbox — task-scoped conversations.
 *
 * The left panel lists only conversations that belong to a task, grouped so that anything
 * waiting on the operator is above everything else. The centre panel is one task's
 * conversation with a composer that can actually change the task's state. The right panel
 * shows real backend data and says "unavailable" rather than inventing a number.
 *
 * Nothing on this screen is authoritative: resumption, verification and task status all come
 * from the API. The client's only jobs are to send the reply and to show what came back.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import clsx from 'clsx'
import {
  AlertTriangle,
  ArrowLeft,
  ChevronDown,
  ExternalLink,
  Inbox as InboxIcon,
  Loader2,
  MessageSquare,
  RefreshCw,
  Search,
  Send,
  ShieldQuestion,
  Trash2,
} from 'lucide-react'

import { Alert, EmptyState, ErrorState, Loading, ProgressBar, StatusBadge } from '@/components/ui'
import { useLiveEvents } from '@/hooks'
import { api } from '@/services/api'
import type {
  InboxConversation,
  InboxConversationDetail,
  InboxMessage,
  InboxReplyResult,
  WsEvent,
} from '@/types'
import { markSeen, isUnread } from '@/services/inboxSeen'

const ACTIVE_STATUSES = new Set(['READY', 'RETRYING', 'IN_PROGRESS', 'WAITING', 'RESUMING'])
const TERMINAL_STATUSES = new Set(['COMPLETED', 'FAILED', 'CANCELLED'])

type Filter = 'all' | 'needs-reply' | 'active'

// ----------------------------------------------------------------------
// Small formatting helpers
// ----------------------------------------------------------------------
function clock(iso: string | null | undefined): string {
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function ago(iso: string | null | undefined): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

const REASON_LABELS: Record<string, string> = {
  MISSING_AUTHENTICATION: 'Authentication required',
  TELEGRAM_SESSION_UNAVAILABLE: 'Telegram session unavailable',
  BROWSER_SESSION_UNAVAILABLE: 'Browser session unavailable',
  HUMAN_VERIFICATION: 'Human verification',
  MISSING_CONFIGURATION: 'Configuration missing',
  AMBIGUOUS_INSTRUCTION: 'Instruction unclear',
  OPERATOR_PAUSE: 'Paused by operator',
  HUMAN_APPROVAL: 'Approval required',
  UNKNOWN: 'Unclassified blocker',
}

/** What the backend said it did with a reply, in words that cannot overstate it. */
const OUTCOME_TONES: Record<string, { tone: 'success' | 'warning' | 'info' | 'error'; label: string }> = {
  RESUMED: { tone: 'success', label: 'Task resumed' },
  CLARIFIED: { tone: 'info', label: 'Instruction recorded' },
  CANCELLED: { tone: 'warning', label: 'Task cancelled' },
  NEEDS_MORE_INFO: { tone: 'warning', label: 'Still blocked' },
  RECORDED: { tone: 'info', label: 'Recorded' },
  IGNORED: { tone: 'error', label: 'Not applied' },
}

// ======================================================================
// Left panel
// ======================================================================
function ConversationRow({
  conversation,
  unread,
  selected,
  onSelect,
}: {
  conversation: InboxConversation
  unread: boolean
  selected: boolean
  onSelect: () => void
}) {
  const status = conversation.task_status
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? 'true' : undefined}
        className={clsx(
          'w-full rounded-xl border px-3 py-2.5 text-left transition-colors',
          'min-h-[44px]',
          selected
            ? 'border-accent bg-accent/10'
            : 'border-surface-300/60 bg-surface-100 hover:bg-surface-200',
        )}
      >
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold text-slate-100">
              {conversation.title ?? 'Untitled task'}
            </p>
            <p className="truncate font-mono text-[11px] text-slate-500">
              {conversation.task_id ?? 'no task'}
            </p>
          </div>
          {conversation.requires_reply && (
            <span
              className="shrink-0 rounded-full bg-status-warning/20 px-2 py-0.5 text-[10px] font-bold uppercase text-status-warning"
              title="This task is waiting for your reply"
            >
              Reply
            </span>
          )}
          {unread && !conversation.requires_reply && (
            <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-accent" aria-label="Unread" />
          )}
        </div>

        {conversation.last_message_preview && (
          <p className="mt-1.5 line-clamp-2 text-xs text-slate-400">
            {conversation.last_message_preview}
          </p>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-2">
          {status ? <StatusBadge status={status} /> : null}
          <span className="text-[11px] text-slate-500">
            {ago(conversation.last_message_at) || 'no messages yet'}
          </span>
        </div>
      </button>
    </li>
  )
}

function ConversationList({
  conversations,
  selectedId,
  search,
  onSearch,
  filter,
  onFilter,
  onSelect,
  isLoading,
  error,
  onRetry,
}: {
  conversations: InboxConversation[]
  selectedId: string | null
  search: string
  onSearch: (value: string) => void
  filter: Filter
  onFilter: (value: Filter) => void
  onSelect: (id: string) => void
  isLoading: boolean
  error: unknown
  onRetry: () => void
}) {
  const unread = useMemo(
    () =>
      new Set(
        conversations.filter((c) => isUnread(c.conversation_id, c.last_message_at)).map((c) => c.conversation_id),
      ),
    [conversations],
  )

  const groups = useMemo(() => {
    const needsReply = conversations.filter((c) => c.requires_reply)
    const rest = conversations.filter((c) => !c.requires_reply)
    const active = rest.filter((c) => c.task_status && ACTIVE_STATUSES.has(c.task_status))
    const recent = rest.filter(
      (c) => !c.task_status || (!ACTIVE_STATUSES.has(c.task_status) && !TERMINAL_STATUSES.has(c.task_status)),
    )
    const settled = rest.filter((c) => c.task_status && TERMINAL_STATUSES.has(c.task_status))
    return [
      { key: 'needs-reply', label: 'Needs Your Reply', items: needsReply },
      { key: 'active', label: 'Active Tasks', items: active },
      { key: 'recent', label: 'Recent Conversations', items: [...recent, ...settled] },
    ]
  }, [conversations])

  const visible = groups
    .map((g) => ({ ...g, items: g.items.filter((c) => (filter === 'all' ? true : filter === 'needs-reply' ? c.requires_reply : c.task_status && ACTIVE_STATUSES.has(c.task_status))) }))
    .filter((g) => g.items.length > 0)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 px-3 pb-3 pt-3">
        <label className="relative block">
          <span className="sr-only">Search conversations</span>
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
          <input
            type="search"
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="Search task or ID"
            className="input w-full pl-9"
          />
        </label>
        <div role="tablist" aria-label="Filter conversations" className="flex gap-1.5">
          {(
            [
              { id: 'all', label: 'All' },
              { id: 'needs-reply', label: 'Needs reply' },
              { id: 'active', label: 'Active' },
            ] as Array<{ id: Filter; label: string }>
          ).map((option) => (
            <button
              key={option.id}
              role="tab"
              type="button"
              aria-selected={filter === option.id}
              onClick={() => onFilter(option.id)}
              className={clsx(
                'rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors',
                filter === option.id
                  ? 'bg-accent/15 text-accent'
                  : 'bg-surface-200 text-slate-400 hover:text-slate-200',
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {isLoading ? (
          <Loading label="Loading conversations" />
        ) : error ? (
          <ErrorState error={error} onRetry={onRetry} />
        ) : visible.length === 0 ? (
          <EmptyState
            icon={<InboxIcon className="h-8 w-8" />}
            title={conversations.length === 0 ? 'No task conversations yet' : 'Nothing matches'}
            description={
              conversations.length === 0
                ? 'A conversation appears here when a task needs your input. Ordinary task activity stays in the Task Manager.'
                : 'Try a different search or filter.'
            }
          />
        ) : (
          <div className="space-y-4">
            {visible.map((group) => (
              <section key={group.key} aria-labelledby={`group-${group.key}`}>
                <h2
                  id={`group-${group.key}`}
                  className="mb-1.5 px-1 text-[10px] font-semibold uppercase tracking-widest text-slate-500"
                >
                  {group.label} ({group.items.length})
                </h2>
                <ul className="space-y-1.5">
                  {group.items.map((conversation) => (
                    <ConversationRow
                      key={conversation.conversation_id}
                      conversation={conversation}
                      unread={unread.has(conversation.conversation_id)}
                      selected={conversation.conversation_id === selectedId}
                      onSelect={() => onSelect(conversation.conversation_id)}
                    />
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ======================================================================
// Centre panel
// ======================================================================
function MessageBubble({ message }: { message: InboxMessage }) {
  const fromUser = message.role === 'user'
  const isIntervention = message.kind === 'INTERVENTION_REQUEST'
  return (
    <li className={clsx('flex', fromUser ? 'justify-end' : 'justify-start')}>
      <div
        className={clsx(
          'max-w-[85%] rounded-2xl px-3 py-2 lg:max-w-[75%]',
          fromUser
            ? 'rounded-br-md bg-accent text-white'
            : isIntervention
              ? 'border border-status-warning/40 bg-status-warning/10'
              : 'border border-surface-300 bg-surface-200',
        )}
      >
        {isIntervention && (
          <p className="mb-1.5 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-status-warning">
            <ShieldQuestion className="h-3.5 w-3.5" />
            Action needed
          </p>
        )}
        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{message.content}</p>
        <p className={clsx('mt-1 text-[11px]', fromUser ? 'text-white/70' : 'text-slate-500')}>
          {fromUser ? 'You' : 'Agent'} · {clock(message.created_at)}
        </p>
      </div>
    </li>
  )
}

function OpenRequestCard({ detail }: { detail: InboxConversationDetail }) {
  const request = detail.open_request
  if (!request) return null
  return (
    <div className="mx-3 mb-3 rounded-xl border border-status-warning/40 bg-status-warning/10 p-3">
      <p className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wide text-status-warning">
        <AlertTriangle className="h-4 w-4" />
        {REASON_LABELS[request.reason_code] ?? request.reason_code}
      </p>
      <p className="mt-1.5 text-sm text-slate-100">{request.problem_summary}</p>
      {request.blocking_step && (
        <p className="mt-1 text-xs text-slate-400">Blocked on: {request.blocking_step}</p>
      )}
      {request.required_action && (
        <p className="mt-1.5 text-sm text-slate-200">{request.required_action}</p>
      )}
      {request.instructions.length > 0 && (
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs text-slate-300">
          {request.instructions.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      )}
      {request.requires_explicit_authorization && (
        <p className="mt-2 text-xs font-medium text-status-warning">
          This one needs an explicit instruction — an ordinary reply will not restart it.
        </p>
      )}
    </div>
  )
}

function OutcomeNotice({ result }: { result: InboxReplyResult }) {
  const meta = OUTCOME_TONES[result.status] ?? { tone: 'info' as const, label: result.status }
  const verification = result.verification
  return (
    <Alert
      kind={meta.tone === 'info' ? 'info' : meta.tone}
      title={meta.label}
    >
      {verification && (
        <p className="text-xs">
          Check: <span className="font-mono">{verification.method}</span> —{' '}
          {verification.checkable
            ? verification.ok
              ? 'confirmed resolved.'
              : 'not resolved yet.'
            : 'could not be run from the server, so nothing was assumed.'}
        </p>
      )}
      {result.detail && <p className="text-xs">{result.detail}</p>}
    </Alert>
  )
}

function Composer({
  conversationId,
  disabled,
  onSent,
}: {
  conversationId: string
  disabled?: boolean
  onSent: (result: InboxReplyResult) => void
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // A ref, not state: the double-submit guard has to be true the instant the click lands,
  // before React has re-rendered the disabled button.
  const inFlight = useRef(false)

  const send = useCallback(async () => {
    const body = text.trim()
    if (!body || inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(null)
    // Stable for the life of this send, so a retry of the same request is deduplicated
    // by the backend rather than queueing the task twice.
    const token = `${conversationId}:${Date.now()}:${Math.random().toString(36).slice(2)}`
    try {
      const result = await api.sendInboxMessage(conversationId, body, token)
      setText('')
      onSent(result)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The reply could not be sent.')
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }, [conversationId, onSent, text])

  return (
    <form
      className="border-t border-surface-300 bg-surface-100 px-3 py-3"
      onSubmit={(e) => {
        e.preventDefault()
        void send()
      }}
    >
      <label htmlFor="inbox-composer" className="sr-only">
        Reply to the agent about this task
      </label>
      <textarea
        id="inbox-composer"
        ref={textareaRef}
        rows={2}
        value={text}
        disabled={disabled || busy}
        enterKeyHint="send"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            void send()
          }
        }}
        placeholder="Reply to the agent about this task…"
        className="input max-h-40 w-full resize-none"
      />
      <div className="mt-2 flex items-center justify-between gap-2">
        <p className="text-[11px] text-slate-500">Enter sends · Shift+Enter for a new line</p>
        <button
          type="submit"
          disabled={disabled || busy || !text.trim()}
          className="btn-primary min-h-[40px] shrink-0"
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          {busy ? 'Sending' : 'Send'}
        </button>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-xs text-status-error">
          {error}
        </p>
      )}
    </form>
  )
}

function ConversationPane({
  detail,
  isLoading,
  error,
  onRetry,
  onSent,
  onDeleted,
  liveEvent,
}: {
  detail: InboxConversationDetail | undefined
  isLoading: boolean
  error: unknown
  onRetry: () => void
  onSent: (result: InboxReplyResult) => void
  onDeleted: (conversationId: string) => void
  liveEvent: string | null
}) {
  const bottomRef = useRef<HTMLDivElement>(null)

  // Removes the conversation only. The task it belongs to is left exactly as it is, and a
  // later request for the same task opens a new conversation.
  const remove = useMutation({
    mutationFn: (conversationId: string) => api.deleteInboxConversation(conversationId),
    onSuccess: (_result, conversationId) => onDeleted(conversationId),
  })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [detail?.messages.length])

  if (isLoading && !detail) return <Loading label="Opening conversation" />
  if (error && !detail) {
    return (
      <div className="p-4">
        <ErrorState error={error} onRetry={onRetry} />
      </div>
    )
  }
  if (!detail) {
    return (
      <EmptyState
        icon={<MessageSquare className="h-8 w-8" />}
        title="Select a conversation"
        description="Pick a task on the left to read what it needs and reply."
      />
    )
  }

  const status = detail.task?.status ?? detail.task_status
  // The server refuses to delete a conversation that is still waiting on the operator while its
  // task is unfinished, because that would strand the task. Mirror it so the button explains
  // itself instead of failing.
  const waitingOnYou = detail.requires_reply && !(status && TERMINAL_STATUSES.has(status))

  const handleDelete = () => {
    if (waitingOnYou || remove.isPending) return
    const label = detail.task_id ? ` for task ${detail.task_id}` : ''
    const confirmed = window.confirm(
      `Delete this conversation${label}? Its messages are removed permanently. ` +
        'The task itself is not changed. If it needs your input again, a new conversation is opened.',
    )
    if (confirmed) remove.mutate(detail.conversation_id)
  }

  return (
    <>
      <header className="border-b border-surface-300 bg-surface-100 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          {status ? <StatusBadge status={status} /> : null}
          <span className="font-mono text-xs text-slate-500">{detail.task_id ?? 'no task'}</span>
          {detail.requires_reply && (
            <span className="ml-auto rounded-full bg-status-warning/20 px-2 py-0.5 text-[11px] font-semibold text-status-warning">
              Waiting for your reply
            </span>
          )}
          <button
            type="button"
            onClick={handleDelete}
            disabled={waitingOnYou || remove.isPending}
            title={
              waitingOnYou
                ? 'Reply to this request or cancel the task before deleting the conversation.'
                : 'Delete this conversation. The task is not changed.'
            }
            className={clsx(
              'flex min-h-[32px] items-center gap-1.5 rounded-md border border-surface-300 px-2 py-1 text-xs text-slate-300 hover:border-status-error/60 hover:text-status-error disabled:cursor-not-allowed disabled:opacity-50',
              !detail.requires_reply && 'ml-auto',
            )}
          >
            {remove.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Trash2 className="h-3.5 w-3.5" />
            )}
            Delete
          </button>
        </div>
        {remove.isError && (
          <div className="mt-2">
            <Alert kind="error">
              {remove.error instanceof Error ? remove.error.message : 'The conversation could not be deleted.'}
            </Alert>
          </div>
        )}
        <h2 className="mt-1 truncate text-base font-semibold text-slate-100">
          {detail.title ?? 'Untitled task'}
        </h2>
        {liveEvent && (
          <p className="mt-1 flex items-center gap-1.5 text-[11px] text-accent">
            <RefreshCw className="h-3 w-3 animate-spin" />
            {liveEvent}
          </p>
        )}
      </header>

      <div className="min-h-[200px] flex-1 overflow-y-auto bg-surface-50 py-3">
        <OpenRequestCard detail={detail} />
        {detail.messages.length === 0 ? (
          <EmptyState title="No messages yet" description="The agent has not reported anything for this task." />
        ) : (
          <ul className="space-y-2.5 px-3">
            {detail.messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))}
          </ul>
        )}
        <div ref={bottomRef} />
      </div>

      {detail.task && <Composer conversationId={detail.conversation_id} onSent={onSent} />}
      {detail.task === null && (
        <div className="border-t border-surface-300 bg-surface-100 px-4 py-3 text-xs text-slate-400">
          This conversation is not linked to a live task, so it is read-only.
        </div>
      )}
    </>
  )
}

// ======================================================================
// Right panel
// ======================================================================
function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <dt className="shrink-0 text-xs text-slate-500">{label}</dt>
      <dd className="min-w-0 break-words text-right text-xs text-slate-200">{value}</dd>
    </div>
  )
}

function Unavailable({ children }: { children: React.ReactNode }) {
  return <span className="text-xs italic text-slate-500">Unavailable — {children}</span>
}

function TaskDetailsPanel({
  detail,
  taskId,
}: {
  detail?: InboxConversationDetail
  taskId: string | null
}) {
  // The authoritative task record lives behind the existing endpoint. The conversation
  // carries a summary; this fetches the full report so nothing here is a second opinion.
  const { data: report, isLoading, error } = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => api.getTask(taskId as string),
    enabled: Boolean(taskId),
  })

  if (!taskId) {
    return <EmptyState title="No task selected" description="Select a conversation to see its task." />
  }
  if (isLoading) return <Loading label="Loading task" />
  if (error) {
    return (
      <div className="p-3">
        <ErrorState error={error} />
      </div>
    )
  }
  if (!report) {
    return (
      <EmptyState title="Task unavailable" description="The backend did not return a task record for this conversation." />
    )
  }

  const latestEvent = report.recent_events?.[0]
  const latestExecution = report.recent_executions?.[0]
  const errorText = typeof report.error_details?.error === 'string' ? report.error_details.error : null

  return (
    <div className="space-y-4 p-3">
      <section>
        <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-widest text-slate-500">
          Status
        </h3>
        <div className="flex items-center gap-2">
          <StatusBadge status={report.status} />
        </div>
        <div className="mt-2">
          <ProgressBar value={typeof report.progress === 'number' ? report.progress : 0} />
          <p className="mt-1 text-[11px] text-slate-500">
            {typeof report.progress === 'number'
              ? `${Math.round(report.progress * 100)}% reported by the backend`
              : 'Progress unavailable — the backend did not report any'}
          </p>
        </div>
      </section>

      <section>
        <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-widest text-slate-500">
          Execution
        </h3>
        <dl className="divide-y divide-surface-300/60">
          <DetailRow
            label="Current step"
            value={report.next_planned_action ?? <Unavailable>no next step recorded</Unavailable>}
          />
          <DetailRow
            label="Last event"
            value={
              latestEvent ? (
                <>
                  {latestEvent.type}
                  {latestEvent.reason ? ` — ${latestEvent.reason}` : ''}
                </>
              ) : (
                <Unavailable>no events recorded</Unavailable>
              )
            }
          />
          <DetailRow
            label="Last error"
            value={
              errorText ? (
                <span className="text-status-error">{errorText}</span>
              ) : (
                <Unavailable>none reported</Unavailable>
              )
            }
          />
          <DetailRow
            label="Retries"
            value={`${report.retry_count} of ${report.max_retries}`}
          />
          <DetailRow
            label="Last run"
            value={
              latestExecution ? (
                <>
                  iteration {latestExecution.iteration} · {latestExecution.status}
                </>
              ) : (
                <Unavailable>no execution recorded</Unavailable>
              )
            }
          />
        </dl>
      </section>

      <section>
        <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-widest text-slate-500">
          Checkpoints &amp; evidence
        </h3>
        <p className="text-xs text-slate-300">
          {report.checkpoints} checkpoint(s), {report.evidence_count} evidence item(s)
        </p>
        {/* The checkpoint written when this task was parked, from the persisted request. */}
        {detail?.open_request?.checkpoint_name && (
          <p className="mt-1 text-[11px] text-slate-500">
            Last saved checkpoint:{' '}
            <span className="font-mono text-slate-300">{detail.open_request.checkpoint_name}</span>
          </p>
        )}
        {detail?.task?.next_execution_at && (
          <p className="mt-1 text-[11px] text-slate-500">
            Next execution: {clock(detail.task.next_execution_at)}
          </p>
        )}
      </section>

      {report.recent_executions?.length > 0 && (
        <section>
          <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-widest text-slate-500">
            Recent execution history
          </h3>
          <ul className="space-y-1">
            {report.recent_executions.slice(0, 5).map((execution) => (
              <li key={execution.iteration} className="rounded-lg bg-surface-200 px-2 py-1.5 text-[11px]">
                <span className="font-mono text-slate-400">#{execution.iteration}</span>{' '}
                <span className="text-slate-200">{execution.status}</span>{' '}
                <span className="text-slate-500">{clock(execution.at)}</span>
                {execution.error && <p className="mt-0.5 text-status-error">{execution.error}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <a
        href={`/tasks?task=${encodeURIComponent(report.task_id)}`}
        className="btn-ghost flex w-full items-center justify-center gap-2"
      >
        <ExternalLink className="h-4 w-4" />
        Open in Task Manager
      </a>
    </div>
  )
}

// ======================================================================
// Page
// ======================================================================
export default function AgentInbox() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('conversation')
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [notice, setNotice] = useState<InboxReplyResult | null>(null)
  const [liveEvent, setLiveEvent] = useState<string | null>(null)

  const listQuery = useQuery({
    queryKey: ['agent-inbox', search],
    queryFn: () => api.listInboxConversations({ search: search || undefined }),
    refetchInterval: 10000,
  })

  const detailQuery = useQuery({
    queryKey: ['agent-inbox-conversation', selectedId],
    queryFn: () => api.getInboxConversation(selectedId as string),
    enabled: Boolean(selectedId),
  })

  // Opening a conversation clears its unread marker, in this tab and for the next one.
  useEffect(() => {
    if (!selectedId) return
    markSeen(selectedId, new Date().toISOString())
  }, [selectedId, detailQuery.data?.messages.length])

  const onWsEvent = useCallback(
    (event: WsEvent) => {
      // The backend publishes a typed event from the API process and relays the worker's
      // events through the existing live log. Both are treated purely as "refetch" hints:
      // the durable record decides what is true, so a missed frame loses nothing.
      const isInbox =
        event.type === 'agent.inbox' ||
        (event.type === 'live.event' && (event.payload as { integration?: string })?.integration === 'agent')
      if (!isInbox) return
      void queryClient.invalidateQueries({ queryKey: ['agent-inbox'] })
      void queryClient.invalidateQueries({ queryKey: ['inbox-summary'] })
      const payload = event.payload as { event?: string; conversation_id?: string }
      if (payload.conversation_id && payload.conversation_id !== selectedId) return
      if (event.type === 'live.event') return
      switch (payload.event) {
        case 'verification.started':
          setLiveEvent('Checking the condition you reported…')
          break
        case 'verification.completed':
          setLiveEvent('Check finished.')
          setTimeout(() => setLiveEvent(null), 4000)
          break
        case 'task.resume_scheduled':
          setLiveEvent('Task queued to resume.')
          setTimeout(() => setLiveEvent(null), 4000)
          break
        case 'task.resumed':
        case 'task.progress':
        case 'task.completed':
        case 'task.failed':
        case 'task.cancelled':
          if (selectedId) void queryClient.invalidateQueries({ queryKey: ['agent-inbox-conversation', selectedId] })
          if (detailQuery.data?.task_id) void queryClient.invalidateQueries({ queryKey: ['task', detailQuery.data.task_id] })
          break
        default:
          if (selectedId) void queryClient.invalidateQueries({ queryKey: ['agent-inbox-conversation', selectedId] })
      }
    },
    [queryClient, selectedId, detailQuery.data?.task_id],
  )

  const { connected } = useLiveEvents(onWsEvent)

  const onSent = useCallback(
    (result: InboxReplyResult) => {
      setNotice(result)
      void queryClient.invalidateQueries({ queryKey: ['agent-inbox'] })
      void queryClient.invalidateQueries({ queryKey: ['inbox-summary'] })
      if (selectedId) void queryClient.invalidateQueries({ queryKey: ['agent-inbox-conversation', selectedId] })
      if (result.task_id) void queryClient.invalidateQueries({ queryKey: ['task', result.task_id] })
    },
    [queryClient, selectedId],
  )

  const onDeleted = useCallback(
    (conversationId: string) => {
      setNotice(null)
      queryClient.removeQueries({ queryKey: ['agent-inbox-conversation', conversationId] })
      void queryClient.invalidateQueries({ queryKey: ['agent-inbox'] })
      void queryClient.invalidateQueries({ queryKey: ['inbox-summary'] })
      // Leave the deleted conversation. The task and its page are unaffected, so nothing
      // under ['task', ...] is invalidated.
      setParams((prev) => {
        const next = new URLSearchParams(prev)
        next.delete('conversation')
        return next
      })
    },
    [queryClient, setParams],
  )

  const select = (id: string) => {
    setNotice(null)
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('conversation', id)
      return next
    })
  }

  const detail = detailQuery.data
  const selectedTaskId = detail?.task_id ?? null

  // Mobile: the list is the landing view and a conversation takes the whole screen.
  return (
    <div className="flex h-full min-h-0 flex-col lg:flex-row">
      <aside
        className={clsx(
          'min-h-0 w-full shrink-0 border-surface-300 lg:w-80 lg:border-r',
          selectedId ? 'hidden lg:flex lg:flex-col' : 'flex flex-1 flex-col',
        )}
        aria-label="Task conversations"
      >
        <div className="flex items-center justify-between gap-2 px-3 pb-1 pt-3 lg:pt-4">
          <h1 className="text-[17px] font-bold text-slate-100 lg:text-2xl">Agent Inbox</h1>
          <span
            className={clsx(
              'rounded-full px-2 py-0.5 text-[11px] font-semibold',
              connected ? 'bg-status-success/15 text-status-success' : 'bg-surface-200 text-slate-500',
            )}
          >
            {connected ? 'Live' : 'Reconnecting'}
          </span>
        </div>
        <ConversationList
          conversations={listQuery.data?.conversations ?? []}
          selectedId={selectedId}
          search={search}
          onSearch={setSearch}
          filter={filter}
          onFilter={setFilter}
          onSelect={select}
          isLoading={listQuery.isLoading}
          error={listQuery.error}
          onRetry={() => void listQuery.refetch()}
        />
      </aside>

      <section
        className={clsx(
          'min-h-0 flex-1 flex-col bg-surface-50 lg:flex',
          selectedId ? 'flex' : 'hidden lg:flex',
        )}
        aria-label="Conversation"
      >
        {selectedId && (
          <button
            type="button"
            onClick={() => navigate(-1)}
            className="flex min-h-[44px] items-center gap-1.5 border-b border-surface-300 bg-surface-100 px-3 text-sm text-slate-300 lg:hidden"
          >
            <ArrowLeft className="h-4 w-4" />
            All conversations
          </button>
        )}
        {notice && (
          <div className="px-3 pt-3">
            <OutcomeNotice result={notice} />
          </div>
        )}
        <ConversationPane
          detail={detail}
          isLoading={detailQuery.isLoading}
          error={detailQuery.error}
          onRetry={() => void detailQuery.refetch()}
          onSent={onSent}
          onDeleted={onDeleted}
          liveEvent={liveEvent}
        />
        <TaskDetailsDrawer taskId={selectedTaskId} />
      </section>

      <aside
        className="hidden min-h-0 w-80 shrink-0 overflow-y-auto border-l border-surface-300 bg-surface-100 xl:block"
        aria-label="Task details"
      >
        <h2 className="px-3 pb-1 pt-4 text-[10px] font-semibold uppercase tracking-widest text-slate-500">
          Task details
        </h2>
        <TaskDetailsPanel detail={detail} taskId={selectedTaskId} />
      </aside>
    </div>
  )
}

/** Narrow screens: task details collapse into an expandable section under the chat. */
function TaskDetailsDrawer({ taskId }: { taskId: string | null }) {
  if (!taskId) return null
  return (
    <details
      role="group"
      aria-label="Task details"
      className="max-h-[55vh] shrink-0 overflow-y-auto border-t border-surface-300 bg-surface-100 xl:hidden"
    >
      <summary className="sticky top-0 flex min-h-[44px] cursor-pointer items-center gap-2 bg-surface-100 px-3 text-sm font-medium text-slate-300">
        <ChevronDown className="h-4 w-4" />
        Task details
      </summary>
      <TaskDetailsPanel taskId={taskId} />
    </details>
  )
}