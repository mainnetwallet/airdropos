/** Live Worker: is the background worker running, what is it doing, and why might tasks be stuck. */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Bot, CheckCircle2, Copy, Send, XCircle } from 'lucide-react'
import clsx from 'clsx'
import { api } from '@/services/api'
import { Card, ErrorState, IconTile, Loading, PageHeader, Pill, SegmentedTabs } from '@/components/ui'
import type { WorkerLogLine, WorkerStatus } from '@/types'

const LEVEL_TONE: Record<string, string> = {
  ERROR: 'text-status-error',
  CRITICAL: 'text-status-error',
  WARNING: 'text-status-warning',
  INFO: 'text-slate-300',
  DEBUG: 'text-slate-500',
}

function formatTime(iso?: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString()
}

function formatDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}

function uptimeSeconds(startedAt?: string | null): number | null {
  if (!startedAt) return null
  const started = new Date(startedAt).getTime()
  return Number.isNaN(started) ? null : Math.max(0, Math.floor((Date.now() - started) / 1000))
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="text-slate-500">{label}</span>
      <span className="text-right text-slate-200">{value}</span>
    </div>
  )
}

function Check({ ok, label }: { ok: boolean; label: string }) {
  return (
    <li className="flex items-center gap-2 text-sm">
      {ok ? (
        <CheckCircle2 className="h-4 w-4 shrink-0 text-status-success" />
      ) : (
        <XCircle className="h-4 w-4 shrink-0 text-status-error" />
      )}
      <span className={ok ? 'text-slate-300' : 'text-slate-400'}>{label}</span>
    </li>
  )
}

function LogView({ logs }: { logs: WorkerLogLine[] }) {
  const [problemsOnly, setProblemsOnly] = useState(false)
  const [copied, setCopied] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)

  const visible = useMemo(
    () => (problemsOnly ? logs.filter((l) => ['WARNING', 'ERROR', 'CRITICAL'].includes(l.level)) : logs),
    [logs, problemsOnly],
  )

  // Follow the newest line, unless the operator scrolled up to read.
  useEffect(() => {
    const box = boxRef.current
    if (box && stickRef.current) box.scrollTop = box.scrollHeight
  }, [visible])

  const copy = async () => {
    const text = visible
      .map((l) => `${formatTime(l.t)} ${l.level.padEnd(7)} ${l.source} | ${l.message}`)
      .join('\n')
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard blocked: the text is still selectable in the box.
    }
  }

  return (
    <Card
      title={`Live log (${visible.length})`}
      action={
        <div className="flex items-center gap-3 text-xs">
          <label className="flex cursor-pointer items-center gap-1.5 text-slate-400">
            <input
              type="checkbox"
              checked={problemsOnly}
              onChange={(e) => setProblemsOnly(e.target.checked)}
            />
            <span className="lg:hidden">Problems only</span>
            <span className="hidden lg:inline">Warnings & errors only</span>
          </label>
          <button onClick={copy} className="btn-ghost flex items-center gap-1.5 px-2 py-1">
            <Copy className="h-3.5 w-3.5" />
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      }
    >
      <div
        ref={boxRef}
        onScroll={(e) => {
          const el = e.currentTarget
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
        }}
        className="h-[50dvh] overflow-auto rounded-lg bg-surface-200 p-3 font-mono text-xs leading-relaxed lg:h-96"
      >
        {visible.length === 0 ? (
          <p className="text-slate-500">
            {logs.length === 0
              ? 'No log lines yet. They appear here once the worker is running.'
              : 'No warnings or errors.'}
          </p>
        ) : (
          visible.map((line, i) => (
            <div key={`${line.t}-${i}`} className={clsx('whitespace-pre-wrap break-words', LEVEL_TONE[line.level])}>
              <span className="text-slate-600">{formatTime(line.t)}</span>{' '}
              <span className="text-slate-500">{line.source.replace('airdropos.', '')}</span> {line.message}
            </div>
          ))
        )}
      </div>
    </Card>
  )
}

function Banner({ data }: { data: WorkerStatus }) {
  if (!data.alive) {
    return (
      <div className="rounded-xl border border-status-error/40 bg-status-error/10 p-4">
        <div className="flex items-center gap-2 font-semibold text-status-error">
          <XCircle className="h-5 w-5" /> Worker is offline
        </div>
        <p className="mt-1 text-sm text-slate-300">
          {data.heartbeat_age_seconds === null
            ? 'It has never reported in, so queued tasks will not run.'
            : `Last heartbeat ${formatDuration(data.heartbeat_age_seconds)} ago, so queued tasks will not run.`}
        </p>
        <p className="mt-2 text-xs text-slate-400">
          Start everything with <code className="font-mono">py run.py</code>, or the worker alone with{' '}
          <code className="font-mono">python -m backend.app.scheduler.worker</code>, then watch the terminal for errors.
        </p>
      </div>
    )
  }
  const worker = data.worker
  return (
    <div className="rounded-xl border border-status-success/40 bg-status-success/10 p-4">
      <div className="flex items-center gap-2 font-semibold text-status-success">
        <CheckCircle2 className="h-5 w-5" /> Worker is running
      </div>
      <p className="mt-1 text-sm text-slate-300">
        {worker.current_task
          ? `Working on ${worker.current_task.task_id}: ${worker.current_task.title}`
          : (worker.note ?? 'idle')}
      </p>
    </div>
  )
}

type WorkerTab = 'overview' | 'workers' | 'logs'

export default function WorkerLive() {
  const [tab, setTab] = useState<WorkerTab>('overview')
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['worker-status'],
    queryFn: api.workerStatus,
    refetchInterval: 3000,
  })

  if (isLoading) return <Loading label="Checking worker" />
  if (error || !data) return <ErrorState error={error} onRetry={() => refetch()} />

  const { worker, queue, telegram } = data
  // On phones one tab shows at a time; on desktop every section is stacked as before.
  const show = (id: WorkerTab) => clsx(tab !== id && 'hidden lg:block')
  const uptime = formatDuration(uptimeSeconds(worker.started_at))

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title={
          <>
            <span className="lg:hidden">Worker Live</span>
            <span className="hidden lg:inline">Live Worker</span>
          </>
        }
        subtitle={
          <>
            <span className="lg:hidden">Monitor all system workers</span>
            <span className="hidden lg:inline">
              The worker is a separate process that actually runs tasks. This page refreshes every 3 seconds.
            </span>
          </>
        }
      />

      <SegmentedTabs
        value={tab}
        onChange={setTab}
        items={[
          { id: 'overview', label: 'Overview' },
          { id: 'workers', label: 'Workers' },
          { id: 'logs', label: 'Logs' },
        ]}
      />

      <div className={show('overview')}>
        <Banner data={data} />
      </div>

      {data.problems.length > 0 && (
        <div className={clsx('rounded-xl border border-status-warning/40 bg-status-warning/10 p-4', show('overview'))}>
          <div className="flex items-center gap-2 font-semibold text-status-warning">
            <AlertTriangle className="h-5 w-5" /> What is wrong
          </div>
          <ul className="mt-2 space-y-1.5 text-sm text-slate-300">
            {data.problems.map((problem, i) => (
              <li key={i}>• {problem}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Phone summary of the processes this dashboard can actually see */}
      {tab === 'workers' && (
        <Card className="lg:hidden">
          <ul className="divide-y divide-surface-300/70">
            <li className="flex items-center gap-3 pb-3">
              <IconTile tone="green">
                <Bot />
              </IconTile>
              <div className="min-w-0 flex-1">
                <p className="font-semibold text-slate-100">Task worker</p>
                <p className="text-xs text-slate-500">Uptime: {uptime}</p>
              </div>
              <Pill tone={data.alive ? 'green' : 'red'}>{data.alive ? 'Running' : 'Offline'}</Pill>
            </li>
            <li className="flex items-center gap-3 pt-3">
              <IconTile tone="blue">
                <Send />
              </IconTile>
              <div className="min-w-0 flex-1">
                <p className="font-semibold text-slate-100">Telegram client</p>
                <p className="text-xs text-slate-500">{worker.telegram_tools?.length ?? 0} tools registered</p>
              </div>
              <Pill tone={telegram.ready ? 'green' : 'amber'}>{telegram.ready ? 'Ready' : 'Not ready'}</Pill>
            </li>
          </ul>
        </Card>
      )}

      <div className={clsx('grid gap-4 lg:grid-cols-3', tab === 'logs' && 'hidden lg:grid')}>
        <Card title="Task queue" className={show('overview')}>
          <div className="space-y-2">
            <Row label="Ready (waiting)" value={queue.ready} />
            <Row label="In progress" value={queue.in_progress} />
            <Row label="Retrying" value={queue.retrying} />
            <Row label="Needs your action" value={queue.needs_user_action} />
            <Row label="Oldest waiting" value={formatDuration(queue.oldest_waiting_seconds)} />
          </div>
        </Card>

        <Card title="Worker" className={show('workers')}>
          <div className="space-y-2">
            <Row label="ID" value={<span className="font-mono text-xs">{worker.worker_id ?? '—'}</span>} />
            <Row label="Uptime" value={uptime} />
            <Row label="Last heartbeat" value={data.heartbeat_age_seconds === null ? '—' : `${data.heartbeat_age_seconds}s ago`} />
            <Row label="Last poll" value={formatTime(worker.last_poll_at)} />
            <Row label="Claimed / finished" value={`${worker.claimed ?? 0} / ${worker.finished ?? 0}`} />
            <Row
              label="Last task"
              value={worker.last_task ? `${worker.last_task.task_id} (${worker.last_task.outcome})` : '—'}
            />
          </div>
        </Card>

        <Card title="Tools & Telegram" className={show('workers')}>
          <div className="space-y-2">
            <Row label="Tools loaded" value={worker.tool_count ?? 0} />
            <Row label="Telegram tools" value={worker.telegram_tools?.length ?? 0} />
          </div>
          <ul className="mt-3 space-y-1.5 border-t border-surface-300 pt-3">
            <Check ok={telegram.enabled} label="TELEGRAM_USER_ENABLED=true" />
            <Check ok={telegram.api_id_set && telegram.api_hash_set} label="API ID and API hash set" />
            <Check ok={telegram.telethon_installed} label="Telethon installed" />
            <Check ok={telegram.session_file_exists} label="Logged in (session file exists)" />
            <Check ok={(worker.telegram_tools?.length ?? 0) > 0} label="Tools registered in the worker" />
          </ul>
          {telegram.hint && <p className="mt-3 text-xs text-status-warning">{telegram.hint}</p>}
        </Card>
      </div>

      {worker.last_error && (
        <Card title="Last worker error" className={show('overview')}>
          <p className="break-words font-mono text-xs text-status-error">
            {worker.last_error.type}: {worker.last_error.message}
          </p>
          <p className="mt-1 text-[11px] text-slate-500">
            {formatTime(worker.last_error.at)}
            {worker.last_error.task_id ? ` · ${worker.last_error.task_id}` : ''}
          </p>
        </Card>
      )}

      <div className={show('logs')}>
        <LogView logs={data.logs} />
      </div>
    </div>
  )
}
